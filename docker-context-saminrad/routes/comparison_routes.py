# i18n-fix-2026-10: Meldungen auf Englisch umgestellt (apply_interact_i18n.py)
# -*- coding: utf-8 -*-
"""
Ground-Truth-Vergleichs-Routes für RADSAM.
Ermöglicht das Hochladen von NIfTI/NRRD-Masken und den Vergleich mit der generierten Segmentierung.
"""

import os
import shutil
import logging
import tempfile
import traceback
import json
import datetime

import numpy as np
import nibabel as nib
from flask import request, jsonify, url_for
from werkzeug.utils import secure_filename
from scipy.ndimage import zoom
from scipy.spatial import cKDTree

from . import comparison_bp
from services.archive_service import create_archive_folder
import state

try:
    import nrrd
    NRRD_AVAILABLE = True
except ImportError:
    NRRD_AVAILABLE = False

logger = logging.getLogger(__name__)


def _convert_upload_to_nifti(files_from_request, temp_dir, generated_mask_path=None):
    """
    Konvertiert einen Datei-Upload (NIfTI, NRRD oder STL) in eine temporäre
    NIfTI-Datei. STL wird dabei auf das Gitter der generierten Segmentierung
    (generated_mask_path) voxelisiert. Gibt (pfad, fehlermeldung) zurück.
    """
    if 'ground_truth_file' not in files_from_request:
        return None, "No file in upload ('ground_truth_file' missing).", None

    gt_file = files_from_request['ground_truth_file']
    filename = secure_filename(gt_file.filename)
    temp_gt_path = os.path.join(temp_dir, filename)
    gt_file.save(temp_gt_path)

    lower = filename.lower()

    if lower.endswith(".nii.gz") or lower.endswith(".nii"):
        return temp_gt_path, None, None

    if lower.endswith(".nrrd"):
        if not NRRD_AVAILABLE:
            return None, "NRRD support not available (pynrrd not installed).", None
        logger.info("Konvertiere NRRD zu NIfTI (Affine inkl. Ursprung)...")
        # Vorhandenen Loader wiederverwenden: baut die Affine aus 'space directions'
        # UND 'space origin' (der Ursprung darf NICHT verworfen werden, sonst ist die
        # Welt-Position falsch und ein weltraum-korrekter Vergleich unmöglich).
        from services.file_loader import load_nrrd_volume
        data, affine, _spacing, _hdr = load_nrrd_volume(temp_gt_path)
        if data is None:
            return None, "NRRD could not be loaded.", None
        nii_img = nib.Nifti1Image(np.asarray(data), affine)
        nii_path = os.path.join(temp_dir, "converted_from_nrrd.nii.gz")
        nib.save(nii_img, nii_path)
        return nii_path, None, None

    if lower.endswith(".stl"):
        if not generated_mask_path:
            return None, "STL comparison requires a generated segmentation as the target grid.", None
        logger.info("Voxelisiere STL-Ground-Truth auf das Gitter der Segmentierung...")
        nii_path = os.path.join(temp_dir, "ground_truth_from_stl.nii.gz")
        return _stl_to_gt_nifti(temp_gt_path, generated_mask_path, nii_path)

    return None, (f"Unsupported file format: '{filename}'. "
                  "Please upload NIfTI (.nii, .nii.gz), NRRD (.nrrd) or STL (.stl)."), None


def _extract_surface(mask):
    """Extrahiert Oberflächenvoxel einer binären Maske mittels Erosion."""
    from scipy.ndimage import binary_erosion
    eroded = binary_erosion(mask)
    surface = mask ^ eroded  # XOR: nur Rand
    coords = np.argwhere(surface)
    return coords.astype(np.float32)


def _read_stl_triangles(filepath):
    """
    Liest ein STL (binär oder ASCII) und gibt die Dreiecke als (M, 3, 3) float32
    zurück (M Dreiecke, je 3 Eckpunkte, je 3 mm-Koordinaten) oder None.

    Binär vs. ASCII wird über die exakte Dateigröße erkannt (84 + 50*n Bytes),
    NICHT über das 'solid'-Keyword (steht auch in Binär-Headern).
    """
    try:
        with open(filepath, 'rb') as f:
            raw = f.read()
    except Exception as e:
        logger.error(f"STL-Datei nicht lesbar {filepath}: {e}")
        return None

    if len(raw) < 84:
        return None

    tris = None
    n_bin = int(np.frombuffer(raw, dtype='<u4', count=1, offset=80)[0])
    if n_bin > 0 and len(raw) == 84 + n_bin * 50:
        tri_dtype = np.dtype([('normal', '<f4', (3,)), ('v', '<f4', (3, 3)), ('attr', '<u2')])
        records = np.frombuffer(raw, dtype=tri_dtype, count=n_bin, offset=84)
        tris = np.array(records['v'], dtype=np.float32)
    else:
        text = raw.decode('ascii', errors='ignore')
        if 'vertex' in text:
            coords = []
            for line in text.splitlines():
                s = line.strip()
                if s.startswith('vertex'):
                    parts = s.split()
                    if len(parts) >= 4:
                        try:
                            coords.append((float(parts[1]), float(parts[2]), float(parts[3])))
                        except ValueError:
                            pass
            if coords:
                verts = np.array(coords, dtype=np.float32)
                m = (verts.shape[0] // 3) * 3
                if m >= 3:
                    tris = verts[:m].reshape(-1, 3, 3)

    if tris is None or tris.shape[0] == 0:
        return None
    finite = np.all(np.isfinite(tris.reshape(tris.shape[0], -1)), axis=1)
    tris = tris[finite]
    if tris.shape[0] == 0:
        return None
    logger.info(f"STL gelesen: {filepath}. {tris.shape[0]} Dreiecke.")
    return tris


def _rasterize_mesh_to_grid(tris_world, affine, shape):
    """
    Voxelisiert ein Dreiecks-Mesh (Weltkoordinaten in mm) in ein VORGEGEBENES
    Voxelgitter (shape) mit gegebener Affine (voxel -> welt). So liegt die
    resultierende Ground-Truth exakt auf dem Gitter der Segmentierung und ist
    weltraum-korrekt ausgerichtet.

    Verfahren: exakte Vollkörper-Voxelisierung per Ray-Parity entlang Z. Für jede
    Gittersäule (i, j) werden die z-Schnittpunkte des Meshes bestimmt; ein Voxel
    (i, j, k) liegt innen, wenn sein Abtastpunkt k zwischen einem Schnittpunkt-Paar
    liegt (even-odd-Regel). Das entspricht der Voxelzentrum-Konvention der Masken
    (kein Halb-Voxel-Bias). Rückgabe: bool-Maske (nx, ny, nz) oder None.
    """
    try:
        inv = np.linalg.inv(affine)
    except np.linalg.LinAlgError:
        return None

    verts = tris_world.reshape(-1, 3).astype(np.float64)
    homog = np.c_[verts, np.ones(len(verts))]
    tv = (homog @ inv.T)[:, :3].reshape(-1, 3, 3)  # Dreiecke im Voxel-Indexraum

    nx, ny, nz = int(shape[0]), int(shape[1]), int(shape[2])
    eps = 1e-4  # kleine Verschiebung der Abtastpunkte gegen exakte Kanten-/Ecktreffer

    I_list, J_list, Z_list = [], [], []
    for t in tv:
        x0, y0, z0 = t[0]
        x1, y1, z1 = t[1]
        x2, y2, z2 = t[2]
        minx = max(int(np.ceil(min(x0, x1, x2) - eps)), 0)
        maxx = min(int(np.floor(max(x0, x1, x2) - eps)), nx - 1)
        miny = max(int(np.ceil(min(y0, y1, y2) - eps)), 0)
        maxy = min(int(np.floor(max(y0, y1, y2) - eps)), ny - 1)
        if minx > maxx or miny > maxy:
            continue
        denom = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if abs(denom) < 1e-12:
            continue  # in XY entartetes Dreieck
        gi, gj = np.meshgrid(np.arange(minx, maxx + 1), np.arange(miny, maxy + 1), indexing='ij')
        gi = gi.ravel().astype(np.float64) + eps
        gj = gj.ravel().astype(np.float64) + eps
        wa = ((y1 - y2) * (gi - x2) + (x2 - x1) * (gj - y2)) / denom
        wb = ((y2 - y0) * (gi - x2) + (x0 - x2) * (gj - y2)) / denom
        wc = 1.0 - wa - wb
        inside = (wa >= 0) & (wb >= 0) & (wc >= 0)
        if not inside.any():
            continue
        zc = wa[inside] * z0 + wb[inside] * z1 + wc[inside] * z2
        I_list.append((gi[inside] - eps).astype(np.int64))
        J_list.append((gj[inside] - eps).astype(np.int64))
        Z_list.append(zc)

    mask = np.zeros((nx, ny, nz), dtype=bool)
    if not I_list:
        return mask

    I = np.concatenate(I_list)
    J = np.concatenate(J_list)
    Z = np.concatenate(Z_list)

    # Nach Säule (i, j) gruppieren und Schnittpunkte paarweise füllen
    key = I * ny + J
    order = np.argsort(key, kind='stable')
    key_s, I_s, J_s, Z_s = key[order], I[order], J[order], Z[order]
    uniq, starts = np.unique(key_s, return_index=True)
    starts = list(starts) + [len(key_s)]
    for g in range(len(uniq)):
        s, e = starts[g], starts[g + 1]
        zs = np.sort(Z_s[s:e])
        i, j = int(I_s[s]), int(J_s[s])
        for p in range(0, len(zs) - 1, 2):
            k0 = max(int(np.ceil(zs[p])), 0)
            k1 = min(int(np.floor(zs[p + 1])), nz - 1)
            if k1 >= k0:
                mask[i, j, k0:k1 + 1] = True
    return mask


def _dice(a, b):
    """Dice-Koeffizient zweier bool-Masken."""
    a = a.astype(bool)
    b = b.astype(bool)
    s = int(a.sum()) + int(b.sum())
    return (2.0 * int(np.logical_and(a, b).sum()) / s) if s > 0 else 0.0


def _q_from_dice(dsc):
    """Qualitätsstufe aus einem Dice-Wert."""
    return 'gut' if dsc >= 0.5 else ('maessig' if dsc >= 0.2 else 'unsicher')


def _signed_permutations():
    """Die 48 vorzeichenbehafteten Permutationsmatrizen (Achsentausch + Spiegelungen)."""
    from itertools import permutations, product
    mats = []
    axis = ['X', 'Y', 'Z']
    for perm in permutations(range(3)):
        for signs in product((1, -1), repeat=3):
            M = np.zeros((3, 3))
            desc = []
            for r, c in enumerate(perm):
                M[r, c] = signs[r]
                desc.append(f"{axis[r]}←{'+' if signs[r] > 0 else '−'}{axis[c]}")
            mats.append((M, ", ".join(desc)))
    return mats


def _auto_align_mesh_to_mask(tris_world, affine, seg_mask):
    """
    Richtet ein STL-Mesh automatisch an einer Referenz-Segmentierung aus, wenn es
    in einem anderen Achsen-Koordinatensystem vorliegt. Probiert die 24 echten
    Rotationen (OHNE Spiegelung — wichtig für chirale Objekte wie Schrauben) mit
    Zentroid-Translation durch und wählt die mit der besten Überlappung.

    Returns:
        (aligned_tris_world, mask, note_dict) oder (None, None, None) bei Fehler.
        note_dict enthält 'axes' (Beschreibung), 'translation_mm', 'dice'.
    """
    shape = seg_mask.shape
    seg_idx = np.argwhere(seg_mask)
    if seg_idx.shape[0] == 0:
        return None, None, None
    seg_world = (np.c_[seg_idx.astype(float), np.ones(len(seg_idx))] @ affine.T)[:, :3]
    seg_centroid = seg_world.mean(axis=0)

    verts = tris_world.reshape(-1, 3).astype(np.float64)
    try:
        inv = np.linalg.inv(affine)
    except np.linalg.LinAlgError:
        return None, None, None

    nx, ny, nz = int(shape[0]), int(shape[1]), int(shape[2])

    def _inside_fraction(v_world):
        vox = np.floor((np.c_[v_world, np.ones(len(v_world))] @ inv.T)[:, :3]).astype(np.int64)
        m = ((vox[:, 0] >= 0) & (vox[:, 0] < nx) &
             (vox[:, 1] >= 0) & (vox[:, 1] < ny) &
             (vox[:, 2] >= 0) & (vox[:, 2] < nz))
        if not m.any():
            return 0.0, None
        vin = vox[m]
        hit = seg_mask[vin[:, 0], vin[:, 1], vin[:, 2]].sum()
        return float(hit) / float(len(v_world)), None

    # 1) Alle 24 echten Rotationen (ohne Spiegelung) billig nach Trefferanteil ranken
    candidates = []
    for M, desc in _proper_rotations():
        v1 = verts @ M.T
        t = seg_centroid - v1.mean(axis=0)
        frac, _ = _inside_fraction(v1 + t)
        candidates.append((frac, M, desc, t))
    candidates.sort(key=lambda c: c[0], reverse=True)

    # 2) Top-Kandidaten voll voxelisieren und echtes Dice bestimmen
    scored = []
    for frac, M, desc, t in candidates[:3]:
        aligned = (verts @ M.T + t).reshape(-1, 3, 3)
        mask = _rasterize_mesh_to_grid(aligned, affine, shape)
        if mask is None:
            continue
        scored.append((_dice(mask, seg_mask), mask, aligned, desc, t))

    if not scored:
        return None, None, None

    scored.sort(key=lambda s: s[0], reverse=True)
    d, mask, aligned, desc, t = scored[0]
    # Eindeutigkeit: Abstand der besten zur zweitbesten Orientierung. Klein = mehrere
    # Orientierungen passen ähnlich gut -> Ausrichtung ist mehrdeutig, bitte prüfen.
    margin = float(d - scored[1][0]) if len(scored) > 1 else float(d)

    # Feinjustage der Translation über die Solid-Zentroide (ganzzahliger Voxel-Shift)
    try:
        from scipy.ndimage import shift as ndi_shift
        seg_c = np.array(np.nonzero(seg_mask), dtype=float).mean(axis=1)
        m_nz = np.array(np.nonzero(mask), dtype=float)
        if m_nz.shape[1] > 0:
            delta = np.round(seg_c - m_nz.mean(axis=1)).astype(int)
            if np.any(delta != 0):
                shifted = ndi_shift(mask.astype(np.float32), delta, order=0, mode='constant', cval=0.0) > 0.5
                d2 = _dice(shifted, seg_mask)
                if d2 > d:
                    mask, d = shifted, d2
    except Exception as e:
        logger.debug(f"Feinjustage übersprungen: {e}")

    note = {'axes': desc, 'translation_mm': float(np.linalg.norm(t)), 'dice': float(d),
            'margin': margin, 'ambiguous': bool(margin < 0.05)}
    return aligned, mask, note


def _stl_to_gt_nifti(stl_path, generated_mask_path, out_path):
    """
    Wandelt eine Ground-Truth-STL in eine NIfTI-Maske auf dem Gitter der
    generierten Segmentierung um. Liegt die STL im selben Koordinatensystem, wird
    sie direkt voxelisiert; andernfalls wird sie automatisch an der Segmentierung
    ausgerichtet (Achsen-Orientierung + Verschiebung).

    Gibt (pfad, fehlermeldung, notiz) zurück; notiz beschreibt, ob/wie ausgerichtet
    wurde und wie gut (Dice), damit der Nutzer die Umwandlung beurteilen kann.
    """
    tris = _read_stl_triangles(stl_path)
    if tris is None or tris.shape[0] == 0:
        return None, "STL contains no valid triangles.", None

    gen = nib.load(generated_mask_path)
    affine = gen.affine
    shape = gen.shape[:3]
    seg_mask = gen.get_fdata().astype(bool)

    def _qual(dsc):
        return 'gut' if dsc >= 0.5 else ('maessig' if dsc >= 0.2 else 'unsicher')

    # Es gibt KEINE absolute Dice-Schwelle (ein "guter" Dice-Wert hängt von Auflösung
    # und Objektgröße ab). Stattdessen rein VERGLEICHEND entscheiden: der
    # untransformierte Direktvergleich (STL liegt schon im Scan-Koordinatensystem)
    # und die beste Achsen-Ausrichtung werden beide berechnet; genommen wird, was
    # tatsächlich besser mit der Segmentierung übereinstimmt. Der Direktvergleich wird
    # bevorzugt und nur ersetzt, wenn die Ausrichtung die Übereinstimmung spürbar
    # verbessert (nicht "auf Verdacht" transformieren). Da die Ausrichtung nur starr
    # ist (Achsentausch/Spiegelung + Verschiebung, volumenerhaltend), kann sie die
    # Form/Größe nicht verfälschen – ob sie stimmt, bestätigt der visuelle Overlay.
    direct = _rasterize_mesh_to_grid(tris, affine, shape)
    if direct is None:
        return None, "STL could not be voxelized (affine not invertible).", None
    dice_direct = _dice(direct, seg_mask) if int(direct.sum()) > 0 else 0.0

    aligned_tris, aligned_mask, ainfo = _auto_align_mesh_to_mask(tris, affine, seg_mask)
    aligned_dice = ainfo['dice'] if ainfo else -1.0
    improvement = 0.02  # nur transformieren, wenn die Ausrichtung messbar (nicht rauschbedingt) besser ist

    if aligned_mask is not None and aligned_dice > dice_direct + improvement:
        # Ausrichtung stimmt deutlich besser mit der Segmentierung überein
        mask = aligned_mask
        # Feine ICP-Registrierung (schräge Restdrehung ausgleichen)
        icp_angle = 0.0
        gt_icp, icp_info = _icp_refine_mask(mask, seg_mask, affine)
        if icp_info.get('dice_after') is not None and icp_info['dice_after'] > aligned_dice:
            mask, aligned_dice, icp_angle = gt_icp, icp_info['dice_after'], icp_info['icp_angle_deg']
        # 180°-Pol-Korrektur (Dice-basiert)
        flipped_180 = False
        mask_flip, flip_info = _resolve_180_flip(mask, seg_mask, affine)
        if flip_info.get('flipped_180'):
            mask, aligned_dice, flipped_180 = mask_flip, flip_info['dice_after'], True
        icp_txt = f" Fine registration +{icp_angle}°." if icp_angle else ""
        flip_txt = " 180° correction (head moved to the correct end)." if flipped_180 else ""
        q = _qual(aligned_dice)
        qtext = {'gut': "good fit", 'maessig': "plausible, please check visually",
                 'unsicher': "UNCERTAIN – probably does not fit, please check"}[q]
        ambig = " Note: several orientations fit similarly well (e.g. for symmetric objects) – please check visually." if ainfo.get('ambiguous') else ""
        note = {'aligned': True, 'quality': q, 'axes': ainfo['axes'],
                'translation_mm': round(ainfo['translation_mm'], 1), 'overlap_dice': round(aligned_dice, 3),
                'icp_angle_deg': icp_angle, 'flipped_180': flipped_180,
                'message': (f"Auto-alignment applied: axes [{ainfo['axes']}], "
                            f"shift {ainfo['translation_mm']:.1f} mm.{icp_txt}{flip_txt} "
                            f"Dice after alignment ≈ {aligned_dice:.2f} – {qtext}.{ambig}")}
    elif int(direct.sum()) > 0:
        # Direktvergleich ist mindestens so gut -> ohne Transformation vergleichen
        mask = direct
        note = {'aligned': False, 'quality': _qual(dice_direct),
                'message': (f"STL compared in the scan coordinate system without alignment "
                            f"(Dice ≈ {dice_direct:.2f}). Axis alignment brought no improvement.")}
    else:
        # Direkt leer UND keine brauchbare Ausrichtung -> Diagnose
        mesh_min = np.round(tris.reshape(-1, 3).min(axis=0), 1)
        mesh_max = np.round(tris.reshape(-1, 3).max(axis=0), 1)
        corners = np.array([[i, j, k] for i in (0, shape[0]) for j in (0, shape[1]) for k in (0, shape[2])], dtype=float)
        gw = (np.c_[corners, np.ones(len(corners))] @ affine.T)[:, :3]
        return None, (
            "The STL does not overlap the segmentation grid and could not be matched by axis "
            "alignment either. "
            f"STL extent (mm): {mesh_min.tolist()} … {mesh_max.tolist()}; "
            f"Scan extent (mm): {np.round(gw.min(axis=0), 1).tolist()} … {np.round(gw.max(axis=0), 1).tolist()}."
        ), None

    if int(mask.sum()) == 0:
        return None, "STL resulted in an empty mask.", None

    nii = nib.Nifti1Image(mask.astype(np.uint8), affine)
    nib.save(nii, out_path)
    logger.info(f"STL-Ground-Truth: {int(mask.sum())} Voxel auf Gitter {tuple(shape)}. {note['message']}")
    return out_path, None, note


def _resample_mask_to_grid(src_data, src_affine, target_affine, target_shape):
    """
    Weltraum-korrekte Nearest-Neighbor-Umtastung einer bool-Maske vom Quell-Gitter
    (src_affine) auf ein Ziel-Gitter (target_affine, target_shape). Für jedes
    Ziel-Voxel wird über die Affinen das zugehörige Quell-Voxel bestimmt.
    """
    from scipy.ndimage import affine_transform
    try:
        T = np.linalg.inv(src_affine) @ target_affine  # Ziel-Voxel -> Quell-Voxel
    except np.linalg.LinAlgError:
        return None
    out = affine_transform(
        src_data.astype(np.float32), T[:3, :3], offset=T[:3, 3],
        output_shape=tuple(int(s) for s in target_shape), order=0, mode='constant', cval=0.0)
    return out > 0.5


def _proper_rotations():
    """Die 24 echten Rotationen (signierte Permutationen mit det=+1, OHNE Spiegelungen —
    wichtig für chirale Objekte wie Schrauben)."""
    return [(M, desc) for (M, desc) in _signed_permutations() if int(round(np.linalg.det(M))) == 1]


def _icp_refine_mask(gt_mask, seg_mask, affine, max_iter=40, tol=1e-3):
    """
    Feine starre Registrierung (ICP) der grob ausgerichteten GT-Maske an die
    Segmentierung: gleicht eine schräge (nicht-achsenparallele) Rest-Verdrehung aus.
    Rotation + Translation, KEINE Skalierung/Spiegelung (Kabsch/SVD mit det-Korrektur).
    Nur behalten, wenn der Dice steigt. Rückgabe: (maske, info).
    """
    info = {'icp_angle_deg': 0.0, 'dice_before': _dice(gt_mask, seg_mask), 'dice_after': None}
    try:
        src_i = _extract_surface(gt_mask).astype(np.float64)
        tgt_i = _extract_surface(seg_mask).astype(np.float64)
        if src_i.shape[0] < 20 or tgt_i.shape[0] < 20:
            info['dice_after'] = info['dice_before']
            return gt_mask, info

        A3, b3 = affine[:3, :3], affine[:3, 3]
        src = src_i @ A3.T + b3
        tgt = tgt_i @ A3.T + b3
        tree = cKDTree(tgt)
        R_tot, t_tot = np.eye(3), np.zeros(3)
        cur, prev = src.copy(), None
        for _ in range(max_iter):
            dist, idx = tree.query(cur)
            P, Q = cur, tgt[idx]
            Pc, Qc = P - P.mean(0), Q - Q.mean(0)
            U, S, Vt = np.linalg.svd(Pc.T @ Qc)
            d = np.sign(np.linalg.det(Vt.T @ U.T))
            R = Vt.T @ np.diag([1.0, 1.0, d]) @ U.T
            t = Q.mean(0) - R @ P.mean(0)
            cur = cur @ R.T + t
            R_tot = R @ R_tot
            t_tot = R @ t_tot + t
            m = float(dist.mean())
            if prev is not None and abs(prev - m) < tol:
                break
            prev = m

        A = np.eye(4)
        A[:3, :3] = R_tot
        A[:3, 3] = t_tot
        refined = _resample_mask_to_grid(gt_mask, A @ affine, affine, gt_mask.shape)
        if refined is None:
            info['dice_after'] = info['dice_before']
            return gt_mask, info
        d_after = _dice(refined, seg_mask)
        if d_after > info['dice_before']:
            info['icp_angle_deg'] = round(float(np.degrees(np.arccos(
                np.clip((np.trace(R_tot) - 1) / 2.0, -1.0, 1.0)))), 1)
            info['dice_after'] = float(d_after)
            return refined, info
        info['dice_after'] = info['dice_before']
        return gt_mask, info
    except Exception as e:
        logger.debug(f"ICP-Feinregistrierung übersprungen: {e}")
        info['dice_after'] = info['dice_before']
        return gt_mask, info


def _resolve_180_flip(gt_mask, seg_mask, affine, margin=0.02):
    """
    Testet die um 180° verdrehte Pose (Rotation um eine Achse senkrecht zur Längsachse)
    und übernimmt sie NUR, wenn der Dice spürbar (> margin) steigt. Rein Dice-basiert
    (keine Kopf-Form-Annahme) → robust auch bei am Kopf degradierten Schrauben.
    Rückgabe: (maske, info mit 'flipped_180', 'dice_after').
    """
    d_cur = _dice(gt_mask, seg_mask)
    info = {'flipped_180': False, 'dice_after': float(d_cur)}
    try:
        gpi = _extract_surface(gt_mask).astype(np.float64)
        spi = _extract_surface(seg_mask).astype(np.float64)
        if gpi.shape[0] < 20 or spi.shape[0] < 20:
            return gt_mask, info
        A3, b3 = affine[:3, :3], affine[:3, 3]
        gp = gpi @ A3.T + b3
        sp = spi @ A3.T + b3
        c = gp.mean(0)
        _, _, vt = np.linalg.svd(gp - c, full_matrices=False)
        axis = vt[0]
        tmpv = np.array([1.0, 0.0, 0.0])
        if abs(float(np.dot(tmpv, axis))) > 0.9:
            tmpv = np.array([0.0, 1.0, 0.0])
        perp = np.cross(axis, tmpv)
        npn = np.linalg.norm(perp)
        if npn < 1e-9:
            return gt_mask, info
        perp = perp / npn
        Rf = 2.0 * np.outer(perp, perp) - np.eye(3)
        cur = (gp - c) @ Rf.T + c
        tree = cKDTree(sp)
        R_tot, t_tot, prev = np.eye(3), np.zeros(3), None
        for _ in range(40):
            dist, idx = tree.query(cur)
            P, Q = cur, sp[idx]
            Pc, Qc = P - P.mean(0), Q - Q.mean(0)
            U, S, Vt = np.linalg.svd(Pc.T @ Qc)
            dd = np.sign(np.linalg.det(Vt.T @ U.T))
            R = Vt.T @ np.diag([1.0, 1.0, dd]) @ U.T
            t = Q.mean(0) - R @ P.mean(0)
            cur = cur @ R.T + t
            R_tot, t_tot = R @ R_tot, R @ t_tot + t
            m = float(dist.mean())
            if prev is not None and abs(prev - m) < 1e-3:
                break
            prev = m
        Af = np.eye(4); Af[:3, :3] = Rf; Af[:3, 3] = c - Rf @ c
        Ai = np.eye(4); Ai[:3, :3] = R_tot; Ai[:3, 3] = t_tot
        flipped = _resample_mask_to_grid(gt_mask, (Ai @ Af) @ affine, affine, gt_mask.shape)
        if flipped is None or not flipped.any():
            return gt_mask, info
        d_flip = _dice(flipped, seg_mask)
        if d_flip > d_cur + margin:
            info['flipped_180'] = True
            info['dice_after'] = float(d_flip)
            return flipped, info
    except Exception as e:
        logger.debug(f"180°-Flip-Test übersprungen: {e}")
    return gt_mask, info


def _axial_dice_refine(gt_mask, seg_mask, affine, span=8.0, step=0.25, margin=0.01):
    """
    Prüft einen axialen Rest-Versatz entlang der Längsachse (PCA der GT-Oberfläche):
    verschiebt die GT in BEIDE Richtungen (±span mm, Schritt step) und übernimmt den
    besten Shift NUR, wenn der Dice spürbar (> margin) steigt. Bei sauberer Ausrichtung
    (Optimum ~0 mm) bleibt alles unverändert — der Check läuft trotzdem (lückenlos).

    Rückgabe: (maske, info mit 'shifted_axial', 'axial_shift_mm', 'dice_after').
    """
    d_cur = _dice(gt_mask, seg_mask)
    info = {'shifted_axial': False, 'axial_shift_mm': 0.0, 'dice_after': float(d_cur)}
    try:
        gv = np.argwhere(gt_mask)
        sv = np.argwhere(seg_mask)
        if gv.shape[0] < 20 or sv.shape[0] < 20:
            return gt_mask, info
        shape = gt_mask.shape
        A3, b3 = affine[:3, :3], affine[:3, 3]
        gw = gv.astype(np.float64) @ A3.T + b3            # GT-Vollkörper (Welt)
        gpi = _extract_surface(gt_mask).astype(np.float64)
        sp = gpi @ A3.T + b3 if gpi.shape[0] >= 20 else gw
        _, _, vt = np.linalg.svd(sp - sp.mean(0), full_matrices=False)
        axis = vt[0]                                      # Längsachse
        inv = np.linalg.inv(affine)
        A_inv, o_inv = inv[:3, :3], inv[:3, 3]
        shp = np.asarray(shape)
        seg_lin = np.unique(np.ravel_multi_index(sv.T, shape))
        nseg = int(seg_lin.size)

        # Vektorisierte set-basierte Dice-Suche (nur GT-Voxel, KEIN Full-Grid-Resample),
        # Nearest-Voxel (round, wie order=0-Resampler) -> Optimum in BEIDE Richtungen.
        def _cheap_dice(d):
            vv = np.rint((gw + d * axis) @ A_inv.T + o_inv).astype(np.int64)
            ok = np.all((vv >= 0) & (vv < shp), axis=1)
            if not ok.any():
                return 0.0
            g_lin = np.unique(np.ravel_multi_index(vv[ok].T, shape))
            inter = int(np.intersect1d(g_lin, seg_lin, assume_unique=True).size)
            return (2.0 * inter) / (int(g_lin.size) + nseg)
        grid = np.arange(-span, span + 1e-9, step)
        d0 = float(grid[int(np.argmax([_cheap_dice(float(d)) for d in grid]))])
        if abs(d0) < 1e-9:
            return gt_mask, info                          # Optimum bei 0 mm -> nichts zu tun (geprüft)

        # Genau EIN echter NN-Resample am Optimum -> materialisieren + per echtem Dice gaten
        As = np.eye(4); As[:3, 3] = d0 * axis             # Welt-Translation entlang Achse
        cand = _resample_mask_to_grid(gt_mask, As @ affine, affine, shape)
        if cand is not None and cand.any():
            dc = _dice(cand, seg_mask)
            if dc > d_cur + margin:
                info['shifted_axial'] = True
                info['axial_shift_mm'] = round(d0, 2)
                info['dice_after'] = float(dc)
                return cand, info
    except Exception as e:
        logger.debug(f"Axialer Feinabgleich übersprungen: {e}")
    return gt_mask, info


def _rotation_about_axis(axis, theta):
    """3x3-Rotation um eine beliebige Achse (Rodrigues-Formel)."""
    a = np.asarray(axis, dtype=float)
    n = np.linalg.norm(a)
    if n < 1e-12:
        return np.eye(3)
    x, y, z = a / n
    c, s = np.cos(theta), np.sin(theta)
    C = 1.0 - c
    return np.array([
        [c + x * x * C,     x * y * C - z * s, x * z * C + y * s],
        [y * x * C + z * s, c + y * y * C,     y * z * C - x * s],
        [z * x * C - y * s, z * y * C + x * s, c + z * z * C]])


def _resolve_roll(gt_mask, seg_mask, affine, fast=False):
    """
    Sucht den DSC-optimalen **Roll um die Längsachse** der GT und übernimmt ihn nur, wenn der Dice
    steigt (never-worse). Wichtig v.a. bei DEGRADIERTEN Schrauben (fehlende Teile/Inseln → asymmetrisch),
    wo der Roll die Reste rotatorisch zur GT ausrichtet.
      - genau (fast=False): volle Auflösung, grob 360°/10° + fein ±10°/2° (~46 Voll-Resamples).
      - schnell (fast=True): Winkel auf herunterskalierten Masken (Faktor 0.4) grob 15° + fein ±15°/5°
        suchen, dann GENAU EINEN Voll-Resample beim besten Winkel — never-worse am Voll-Dice.
    Rückgabe: (maske, info mit 'roll_deg', 'dice_after').
    """
    d_cur = _dice(gt_mask, seg_mask)
    info = {'roll_deg': 0.0, 'dice_after': float(d_cur)}
    try:
        gpi = _extract_surface(gt_mask).astype(np.float64)
        if gpi.shape[0] < 20:
            return gt_mask, info
        A3, b3 = affine[:3, :3], affine[:3, 3]
        gp = gpi @ A3.T + b3
        c = gp.mean(0)
        _, _, vt = np.linalg.svd(gp - c, full_matrices=False)
        axis = vt[0]

        def _eval_on(gm, sm, aff, ang, cc):
            R = _rotation_about_axis(axis, np.deg2rad(ang))
            Af = np.eye(4); Af[:3, :3] = R; Af[:3, 3] = cc - R @ cc
            rolled = _resample_mask_to_grid(gm, Af @ aff, aff, gm.shape)
            if rolled is None or not rolled.any():
                return None, -1.0
            return rolled, _dice(rolled, sm)

        if fast:
            # Winkel grob auf kleinen Masken suchen (schnell), dann 1x voll anwenden.
            from scipy.ndimage import zoom
            f = 0.4
            gm_s = zoom(gt_mask.astype(np.uint8), f, order=0).astype(bool)
            sm_s = zoom(seg_mask.astype(np.uint8), f, order=0).astype(bool)
            if int(gm_s.sum()) >= 20 and int(sm_s.sum()) >= 20:
                aff_s = affine.copy()
                aff_s[:3, :3] = affine[:3, :3] / f      # kleinere Auflösung -> größeres Voxel-Spacing
                gpi_s = _extract_surface(gm_s).astype(np.float64)
                c_s = (gpi_s @ aff_s[:3, :3].T + aff_s[:3, 3]).mean(0)
                best_d_s, best_ang = -1.0, 0.0
                for ang in np.arange(0.0, 360.0, 15.0):
                    if abs(ang) < 1e-6:
                        continue
                    m, d = _eval_on(gm_s, sm_s, aff_s, float(ang), c_s)
                    if m is not None and d > best_d_s:
                        best_d_s, best_ang = d, float(ang)
                for ang in np.arange(best_ang - 15.0, best_ang + 15.001, 5.0):
                    if abs(ang % 360.0) < 1e-6:
                        continue
                    m, d = _eval_on(gm_s, sm_s, aff_s, float(ang), c_s)
                    if m is not None and d > best_d_s:
                        best_d_s, best_ang = d, float(ang)
                if abs(best_ang % 360.0) > 1e-6:
                    cand, d_full = _eval_on(gt_mask, seg_mask, affine, best_ang, c)  # EIN Voll-Resample
                    if cand is not None and d_full > d_cur + 1e-9:
                        info['roll_deg'] = round(((best_ang + 180.0) % 360.0) - 180.0, 1)
                        info['dice_after'] = float(d_full)
                        return cand, info
            return gt_mask, info

        # genau (Standard): volle Auflösung
        best_mask, best_d, best_ang = gt_mask, d_cur, 0.0
        for ang in list(np.arange(0.0, 360.0, 10.0)):        # grob: voller Umlauf
            if abs(ang) < 1e-6:
                continue
            m, d = _eval_on(gt_mask, seg_mask, affine, float(ang), c)
            if m is not None and d > best_d:
                best_mask, best_d, best_ang = m, d, float(ang)
        for ang in list(np.arange(best_ang - 10.0, best_ang + 10.001, 2.0)):  # fein um das Optimum
            if abs(ang - best_ang) < 1e-6 or abs(ang % 360.0) < 1e-6:
                continue
            m, d = _eval_on(gt_mask, seg_mask, affine, float(ang), c)
            if m is not None and d > best_d:
                best_mask, best_d, best_ang = m, d, float(ang)

        if best_d > d_cur + 1e-9 and abs(best_ang % 360.0) > 1e-6:
            info['roll_deg'] = round(((best_ang + 180.0) % 360.0) - 180.0, 1)  # -180..180
            info['dice_after'] = float(best_d)
            return best_mask, info
    except Exception as e:
        logger.debug(f"Roll-Suche übersprungen: {e}")
    return gt_mask, info


def _maximize_alignment(gt_mask, seg_mask, affine, note, registered_tierB, precision='accurate'):
    """
    DSC-maximierende Feinausrichtung nach der Basis-Ausrichtung, garantiert **never-worse**.
    Vergleicht zwei Kandidaten und nimmt den mit höherem Dice:
      (i)  nur axialer Rest-Versatz-Check
      (ii) ICP (kontinuierliche Rotation) + 180°-Flip + Roll (um Längsachse) + axial
    Kandidat (ii) wird IMMER getestet (never-worse). Ergänzt die alignment_note transparent um das,
    was tatsächlich griff. Rückgabe: (maske, note).
    """
    # Kandidat (i): nur axial (exakt das bisherige Verhalten)
    m0, ax0 = _axial_dice_refine(gt_mask, seg_mask, affine)
    best = {'mask': m0, 'dice': _dice(m0, seg_mask), 'icp': 0.0, 'flip': False, 'roll': 0.0,
            'axial': (ax0.get('axial_shift_mm', 0.0) if ax0.get('shifted_axial') else None)}

    # Kandidat (ii): ICP + 180°-Flip + Roll + axial — IMMER (never-worse abgesichert).
    d_base = _dice(gt_mask, seg_mask)
    m1, ic = _icp_refine_mask(gt_mask, seg_mask, affine)
    m1, fl = _resolve_180_flip(m1, seg_mask, affine)
    m1, rl = _resolve_roll(m1, seg_mask, affine, fast=(precision == 'fast'))
    m1, ax1 = _axial_dice_refine(m1, seg_mask, affine)
    d1 = _dice(m1, seg_mask)
    if d1 > best['dice'] + 1e-9:
        best = {'mask': m1, 'dice': d1,
                'icp': (ic.get('icp_angle_deg', 0.0)
                        if (ic.get('dice_after') is not None and ic['dice_after'] > d_base + 1e-9) else 0.0),
                'flip': bool(fl.get('flipped_180')),
                'roll': float(rl.get('roll_deg', 0.0) or 0.0),
                'axial': (ax1.get('axial_shift_mm', 0.0) if ax1.get('shifted_axial') else None)}

    # Notiz ergänzen (nur was tatsächlich gegriffen hat)
    if best['icp'] or best['flip'] or best['roll'] or best['axial'] is not None:
        if not isinstance(note, dict):
            note = {'aligned': True, 'quality': _q_from_dice(best['dice'])}
        if best['icp']:
            note['icp_angle_deg'] = best['icp']
        if best['flip']:
            note['flipped_180'] = True
        if best['roll']:
            note['roll_deg'] = best['roll']
        if best['axial'] is not None:
            note['axial_shift_mm'] = best['axial']
        note['dice_registered'] = round(best['dice'], 3)
        note['quality'] = _q_from_dice(best['dice'])
        note['message'] = (note.get('message', '') +
            (f" Fine registration +{best['icp']}°." if best['icp'] else "") +
            (" 180° correction (head moved to the correct end)." if best['flip'] else "") +
            (f" Roll {best['roll']:+.1f}° about the long axis." if best['roll'] else "") +
            (f" Residual axial offset {best['axial']:+.2f} mm corrected (Dice → {best['dice']:.2f})."
             if best['axial'] is not None else ""))
    return best['mask'], note


def _rigid_register_voxel_gt(gt_data, gt_affine, seg_mask, seg_affine):
    """
    Registriert eine Voxel-Ground-Truth starr an der Segmentierung, wenn beide in
    verschiedenen (Achsen-)Koordinatensystemen liegen: probiert die 24 echten
    Rotationen + Zentroid-Translation im Weltraum, tastet die beste Variante aufs
    Segmentierungs-Gitter um und feinjustiert die Translation über die Solid-Zentroide.
    KEINE Skalierung, KEINE Spiegelung. Rückgabe: (gt_on_grid bool, note) oder (None, None).
    """
    from scipy.ndimage import shift as ndi_shift
    seg_shape = seg_mask.shape
    seg_idx = np.argwhere(seg_mask)
    gt_idx = np.argwhere(gt_data > 0)
    if seg_idx.shape[0] == 0 or gt_idx.shape[0] == 0:
        return None, None

    seg_world = (np.c_[seg_idx.astype(float), np.ones(len(seg_idx))] @ seg_affine.T)[:, :3]
    gt_world = (np.c_[gt_idx.astype(float), np.ones(len(gt_idx))] @ gt_affine.T)[:, :3]
    seg_centroid = seg_world.mean(axis=0)
    gt_centroid = gt_world.mean(axis=0)
    gt_c = gt_world - gt_centroid
    try:
        inv_seg = np.linalg.inv(seg_affine)
    except np.linalg.LinAlgError:
        return None, None
    nx, ny, nz = int(seg_shape[0]), int(seg_shape[1]), int(seg_shape[2])

    def _inside_fraction(R):
        w = gt_c @ R.T + seg_centroid
        vox = np.floor((np.c_[w, np.ones(len(w))] @ inv_seg.T)[:, :3]).astype(np.int64)
        m = ((vox[:, 0] >= 0) & (vox[:, 0] < nx) & (vox[:, 1] >= 0) & (vox[:, 1] < ny) &
             (vox[:, 2] >= 0) & (vox[:, 2] < nz))
        if not m.any():
            return 0.0
        vin = vox[m]
        return float(seg_mask[vin[:, 0], vin[:, 1], vin[:, 2]].sum()) / float(len(w))

    def _resample_with_rotation(R):
        # Starre Welt-Transformation um die Zentroide: world' = R·(world_gt − gt_c) + seg_c
        A = np.eye(4)
        A[:3, :3] = R
        A[:3, 3] = seg_centroid - R @ gt_centroid
        src_affine_eff = A @ gt_affine  # GT-Voxel -> Welt im Segmentierungs-Rahmen
        return _resample_mask_to_grid(gt_data, src_affine_eff, seg_affine, seg_shape)

    # 1) billig ranken, 2) die besten 2 voll umtasten
    ranked = sorted(((_inside_fraction(M), M, desc) for M, desc in _proper_rotations()),
                    key=lambda c: c[0], reverse=True)
    scored = []
    for frac, M, desc in ranked[:2]:
        gg = _resample_with_rotation(M)
        if gg is not None:
            scored.append((_dice(gg, seg_mask), gg, desc))
    if not scored:
        return None, None
    scored.sort(key=lambda s: s[0], reverse=True)
    d, gg, desc = scored[0]
    margin = float(d - scored[1][0]) if len(scored) > 1 else float(d)

    # 3) Translation feinjustieren (Solid-Zentroide, ganzzahliger Voxel-Shift)
    try:
        seg_c = np.array(np.nonzero(seg_mask), float).mean(1)
        g_nz = np.array(np.nonzero(gg), float)
        if g_nz.shape[1] > 0:
            delta = np.round(seg_c - g_nz.mean(1)).astype(int)
            if np.any(delta != 0):
                sh = ndi_shift(gg.astype(np.float32), delta, order=0, mode='constant', cval=0.0) > 0.5
                d2 = _dice(sh, seg_mask)
                if d2 > d:
                    gg, d = sh, d2
    except Exception as e:
        logger.debug(f"Feinjustage (Voxel-GT) übersprungen: {e}")

    # Feine ICP-Registrierung (schräge Restdrehung ausgleichen)
    icp_angle = 0.0
    gg_icp, icp_info = _icp_refine_mask(gg, seg_mask, seg_affine)
    if icp_info.get('dice_after') is not None and icp_info['dice_after'] > d:
        gg, d, icp_angle = gg_icp, icp_info['dice_after'], icp_info['icp_angle_deg']

    # 180°-Pol-Korrektur (Dice-basiert, robust bei Degradation)
    flipped_180 = False
    gg_flip, flip_info = _resolve_180_flip(gg, seg_mask, seg_affine)
    if flip_info.get('flipped_180'):
        gg, d, flipped_180 = gg_flip, flip_info['dice_after'], True

    note = {'axes': desc, 'dice': float(d), 'margin': margin, 'ambiguous': bool(margin < 0.05),
            'icp_angle_deg': icp_angle, 'flipped_180': flipped_180}
    return gg, note


@comparison_bp.route('/compare_ground_truth', methods=['POST'])
def compare_ground_truth_route():
    """
    Vergleicht eine hochgeladene Ground-Truth-Maske (NIfTI oder NRRD)
    mit der zuletzt generierten Segmentierungsmaske.
    Berechnet Dice, ASSD, Volumendifferenz, Sensitivität, Spezifität, Präzision.
    """
    if 'generated_mask_filename' not in request.form:
        return jsonify({"success": False, "error": "Missing reference file name (generated_mask_filename)."}), 400

    generated_filename = request.form['generated_mask_filename']
    # Vergleichsmodus vom GUI: 'fast' (grobe Roll-Suche, ~schnell) oder 'accurate' (voll, Standard).
    compare_precision = request.form.get('precision', 'accurate')
    if compare_precision not in ('fast', 'accurate'):
        compare_precision = 'accurate'
    temp_comparison_dir = tempfile.mkdtemp(dir=state.TEMP_UPLOAD_FOLDER, prefix="gt_comp_")

    try:
        # 1. Analyse-Maske im Archiv finden (ohne "viewer_"-Prefix).
        #    Wird u.a. als Ziel-Gitter für die STL-Voxelisierung gebraucht.
        archive_folder = create_archive_folder()
        results_folder = os.path.join(archive_folder, "segmentation_results")
        models_folder = os.path.join(results_folder, "3d_models")

        analysis_filename = generated_filename.replace("viewer_", "")
        generated_mask_path = os.path.join(models_folder, analysis_filename)

        if not os.path.exists(generated_mask_path):
            return jsonify({
                "success": False,
                "error": f"Analysis mask '{analysis_filename}' not found in the archive."
            }), 404

        # 2. Ground-Truth zu NIfTI konvertieren (STL wird auf das Gitter der Analyse-Maske voxelisiert)
        gt_nifti_path, error, alignment_note = _convert_upload_to_nifti(request.files, temp_comparison_dir, generated_mask_path)
        if error:
            return jsonify({"success": False, "error": error}), 400

        # 3. Originale GT archivieren
        try:
            gt_original_save_path = os.path.join(models_folder, "ground_truth_original_full_resolution.nii.gz")
            shutil.copy2(gt_nifti_path, gt_original_save_path)
            logger.info(f"Originale Ground-Truth archiviert: {gt_original_save_path}")
        except Exception as e_copy:
            logger.warning(f"Konnte GT-Original nicht archivieren: {e_copy}")

        # 4. Beide Masken laden
        generated_nii = nib.load(generated_mask_path)
        gt_nii = nib.load(gt_nifti_path)

        generated_data = generated_nii.get_fdata().astype(np.bool_)
        gt_data = gt_nii.get_fdata().astype(np.bool_)

        # 5. Volumen berechnen (vor Resampling, jede Maske mit eigener Voxelgröße)
        volume_generated_cm3 = 0.0
        volume_gt_cm3 = 0.0
        volume_diff_percent = None
        try:
            voxel_vol_gen = abs(np.linalg.det(generated_nii.affine[:3, :3]))
            if not np.isfinite(voxel_vol_gen) or voxel_vol_gen < 1e-9:
                voxel_vol_gen = np.prod(generated_nii.header.get_zooms()[:3])

            voxel_vol_gt = abs(np.linalg.det(gt_nii.affine[:3, :3]))
            if not np.isfinite(voxel_vol_gt) or voxel_vol_gt < 1e-9:
                voxel_vol_gt = np.prod(gt_nii.header.get_zooms()[:3])

            volume_generated_cm3 = (generated_data.sum() * voxel_vol_gen) / 1000.0
            volume_gt_cm3 = (gt_data.sum() * voxel_vol_gt) / 1000.0
            if volume_gt_cm3 > 0:
                volume_diff_percent = ((volume_generated_cm3 - volume_gt_cm3) / volume_gt_cm3) * 100

            logger.info(
                f"Volumen: Generiert={volume_generated_cm3:.2f} cm³, GT={volume_gt_cm3:.2f} cm³, "
                f"Diff={volume_diff_percent:.2f}%" if volume_diff_percent is not None else
                f"Volumen: Generiert={volume_generated_cm3:.2f} cm³, GT={volume_gt_cm3:.2f} cm³"
            )
        except Exception as e_vol:
            logger.error(f"Fehler bei Volumenberechnung: {e_vol}")

        # 6. GT weltraum-korrekt auf das Segmentierungs-Gitter bringen.
        #    Die Segmentierung bleibt UNANGETASTET (maßgeblich); nur die GT wird umgetastet.
        #    STL liegt bereits exakt auf dem Gitter (gleiche Affine+Form) -> übersprungen.
        assd_voxel_size = generated_nii.header.get_zooms()[0]

        same_grid = (generated_data.shape == gt_data.shape and
                     np.allclose(generated_nii.affine, gt_nii.affine, atol=1e-4))
        _registered_tierB = False   # True, wenn die 24-Rotationen-Registrierung (inkl. ICP+Flip) lief
        if not same_grid:
            # Tier A: unverzerrte, weltraum-korrekte Umtastung über die Affinen
            gt_on_grid = _resample_mask_to_grid(gt_data, gt_nii.affine,
                                                generated_nii.affine, generated_data.shape)
            dice_affine = _dice(gt_on_grid, generated_data) if (gt_on_grid is not None and gt_on_grid.any()) else 0.0

            if dice_affine >= 0.10:
                # Affinen liegen im selben Rahmen -> unverzerrt übernehmen (kein Bias)
                gt_data = gt_on_grid
                alignment_note = alignment_note or {
                    'aligned': False, 'quality': _q_from_dice(dice_affine),
                    'dice_direct': round(dice_affine, 3),
                    'message': (f"GT resampled onto the segmentation grid in world space (affine, "
                                f"no registration). Overlap Dice ≈ {dice_affine:.2f}.")}
            else:
                # Tier B: starre Registrierung (Rotation + Verschiebung, keine Skalierung/Spiegelung)
                logger.info(f"Affine-Überlappung gering (Dice {dice_affine:.2f}) – starre Registrierung ...")
                gg, rinfo = _rigid_register_voxel_gt(gt_data, gt_nii.affine, generated_data, generated_nii.affine)
                if gg is not None and rinfo is not None and rinfo['dice'] > dice_affine:
                    gt_data = gg
                    _registered_tierB = True
                    q = _q_from_dice(rinfo['dice'])
                    qtext = {'gut': "good fit", 'maessig': "plausible, please check visually",
                             'unsicher': "UNCERTAIN – please check"}[q]
                    ambig = (" Note: several orientations fit similarly well (e.g. symmetric objects) – please check visually."
                             if rinfo.get('ambiguous') else "")
                    icp_txt = f" Fine registration +{rinfo['icp_angle_deg']}°." if rinfo.get('icp_angle_deg') else ""
                    flip_txt = " 180° correction (head moved to the correct end)." if rinfo.get('flipped_180') else ""
                    alignment_note = {
                        'aligned': True, 'quality': q, 'axes': rinfo['axes'],
                        'dice_direct': round(dice_affine, 3), 'dice_registered': round(rinfo['dice'], 3),
                        'icp_angle_deg': rinfo.get('icp_angle_deg', 0.0), 'flipped_180': bool(rinfo.get('flipped_180')),
                        'message': (f"GT was in a different coordinate system. Rigid registration "
                                    f"applied (rotation [{rinfo['axes']}], no scaling/mirroring).{icp_txt}{flip_txt} "
                                    f"Dice without alignment ≈ {dice_affine:.2f}, after registration ≈ "
                                    f"{rinfo['dice']:.2f} – {qtext}.{ambig}")}
                elif gt_on_grid is not None and gt_on_grid.any():
                    gt_data = gt_on_grid  # nichts Besseres gefunden -> affine-Ergebnis
                    alignment_note = alignment_note or {
                        'aligned': False, 'quality': _q_from_dice(dice_affine),
                        'dice_direct': round(dice_affine, 3),
                        'message': (f"GT could only be matched weakly (Dice ≈ {dice_affine:.2f}). "
                                    f"Please check the coordinate system/registration of the GT.")}
                else:
                    # Weder affine noch Registrierung ergeben Überlappung -> Diagnose
                    gm = (np.c_[np.argwhere(gt_data > 0).astype(float), np.ones(int((gt_data > 0).sum()))] @ gt_nii.affine.T)[:, :3]
                    sm = (np.c_[np.argwhere(generated_data).astype(float), np.ones(int(generated_data.sum()))] @ generated_nii.affine.T)[:, :3]
                    return jsonify({"success": False, "error": (
                        "GT and segmentation do not overlap and could not be matched rigidly either. "
                        f"GT world bbox: {np.round(gm.min(0), 1).tolist()} … {np.round(gm.max(0), 1).tolist()}; "
                        f"Segmentation world bbox: {np.round(sm.min(0), 1).tolist()} … {np.round(sm.max(0), 1).tolist()}."
                    )}), 400

        if generated_data.shape != gt_data.shape:
            return jsonify({
                "success": False,
                "error": (f"Dimension error: generated mask {generated_data.shape} and "
                          f"ground truth {gt_data.shape} do not match after alignment.")
            }), 400

        # 6a. DSC-maximierende Feinausrichtung für JEDEN Vergleich, garantiert never-worse:
        #     max(Dice) aus (i) nur axial und (ii) ICP + 180°-Flip + axial. (ii) entfällt, wenn Tier B lief.
        gt_data, alignment_note = _maximize_alignment(
            gt_data, generated_data, generated_nii.affine, alignment_note, _registered_tierB,
            precision=compare_precision)

        # 7. Metriken berechnen
        intersection = np.logical_and(generated_data, gt_data).sum()
        sum_both = generated_data.sum() + gt_data.sum()
        dice_score = (2.0 * intersection) / sum_both if sum_both > 0 else 0.0

        true_positives = int(intersection)
        false_positives = int(generated_data.sum() - true_positives)
        false_negatives = int(gt_data.sum() - true_positives)
        true_negatives = int(generated_data.size - (true_positives + false_positives + false_negatives))

        sensitivity = (true_positives / (true_positives + false_negatives) * 100) if (true_positives + false_negatives) > 0 else 0.0
        specificity = (true_negatives / (true_negatives + false_positives) * 100) if (true_negatives + false_positives) > 0 else 0.0
        precision = (true_positives / (true_positives + false_positives) * 100) if (true_positives + false_positives) > 0 else 0.0

        # 8. ASSD berechnen
        surface_generated = _extract_surface(generated_data)
        surface_gt = _extract_surface(gt_data)
        assd_mm = 0.0
        if surface_generated.size > 0 and surface_gt.size > 0:
            tree_gt = cKDTree(surface_gt)
            dist_gen_to_gt, _ = tree_gt.query(surface_generated, k=1)
            tree_gen = cKDTree(surface_generated)
            dist_gt_to_gen, _ = tree_gen.query(surface_gt, k=1)
            assd = (dist_gen_to_gt.sum() + dist_gt_to_gen.sum()) / (len(surface_generated) + len(surface_gt))
            assd_mm = assd * assd_voxel_size

        logger.info(f"Metriken: Dice={dice_score:.4f}, ASSD={assd_mm:.4f} mm")

        # 9. Downsampled GT für Viewer speichern
        viewer_downsample = state.VIEWER_SPECIFIC_DOWNSAMPLE_FACTOR
        downsampled_gt_data = zoom(gt_data.astype(np.uint8), viewer_downsample, order=0, mode='nearest')

        viewer_generated_path = os.path.join(models_folder, generated_filename)
        viewer_ref_nii = nib.load(viewer_generated_path)

        downsampled_gt_nii = nib.Nifti1Image(downsampled_gt_data, viewer_ref_nii.affine, header=viewer_ref_nii.header)
        gt_viewer_filename = f"viewer_gt_{os.path.basename(gt_nifti_path)}.nii.gz"
        gt_viewer_path = os.path.join(models_folder, gt_viewer_filename)
        nib.save(downsampled_gt_nii, gt_viewer_path)

        gt_viewer_url = url_for('image.serve_nifti_mask_sam', filename=gt_viewer_filename)

        # 9b. Kombinierte Vergleichs-Labelmap für den Viewer:
        #     1 = nur Segmentierung (grün), 2 = nur Ground-Truth (rot), 3 = Überlappung (gelb).
        #     Ein EINZIGES Volumen rendert Niivue zuverlässig in 3D (zwei deckungsgleiche
        #     Voll-Körper verdecken sich sonst gegenseitig).
        comparison_overlay_url = None
        try:
            combined = np.zeros(generated_data.shape, dtype=np.uint8)
            combined[generated_data & ~gt_data] = 1
            combined[gt_data & ~generated_data] = 2
            combined[generated_data & gt_data] = 3
            combined_ds = zoom(combined, viewer_downsample, order=0, mode='nearest').astype(np.uint8)
            combined_nii = nib.Nifti1Image(combined_ds, viewer_ref_nii.affine)
            combined_nii.header.set_data_dtype(np.uint8)
            comparison_overlay_filename = f"comparison_overlay_{os.path.basename(gt_nifti_path)}.nii.gz"
            nib.save(combined_nii, os.path.join(models_folder, comparison_overlay_filename))
            comparison_overlay_url = url_for('image.serve_nifti_mask_sam', filename=comparison_overlay_filename)
            logger.info(f"Vergleichs-Labelmap: nur-Seg={int((combined==1).sum())}, "
                        f"nur-GT={int((combined==2).sum())}, Overlap={int((combined==3).sum())}")
        except Exception as e_ov:
            logger.error(f"Konnte Vergleichs-Labelmap nicht erstellen: {e_ov}")

        # 10. Ergebnisse in JSON-Parameterdatei speichern
        try:
            params_file = os.path.join(results_folder, "segmentation_parameters.json")
            if os.path.exists(params_file):
                with open(params_file, 'r', encoding='utf-8') as f:
                    params_data = json.load(f)
                params_data['ground_truth_comparison'] = {
                    'timestamp': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    'dice_score': float(dice_score),
                    'assd_mm': float(assd_mm),
                    'volume_generated_cm3': float(volume_generated_cm3),
                    'volume_ground_truth_cm3': float(volume_gt_cm3),
                    'volume_difference_percent': float(volume_diff_percent) if volume_diff_percent is not None else None,
                    'true_positives_voxels': true_positives,
                    'false_positives_voxels': false_positives,
                    'false_negatives_voxels': false_negatives,
                    'sensitivity_percent': round(sensitivity, 2),
                    'specificity_percent': round(specificity, 2),
                    'precision_percent': round(precision, 2),
                    'alignment': alignment_note if alignment_note else None,
                }
                with open(params_file, 'w', encoding='utf-8') as f:
                    json.dump(params_data, f, indent=4, ensure_ascii=False)
        except Exception as e_json:
            logger.error(f"Fehler beim Speichern der GT-Vergleichsergebnisse: {e_json}")

        return jsonify({
            "success": True,
            "dice_score": float(dice_score),
            "assd_mm": float(assd_mm),
            "volume_diff_cm3": float(volume_diff_percent) if volume_diff_percent is not None else None,
            "ground_truth_viewer_url": gt_viewer_url,
            "comparison_overlay_url": comparison_overlay_url,
            "true_positives_voxels": true_positives,
            "false_positives_voxels": false_positives,
            "false_negatives_voxels": false_negatives,
            "sensitivity_percent": round(sensitivity, 2),
            "specificity_percent": round(specificity, 2),
            "precision_percent": round(precision, 2),
            "alignment_note": alignment_note.get('message') if alignment_note else None,
            "alignment_quality": alignment_note.get('quality') if alignment_note else None,
            "dice_direct": alignment_note.get('dice_direct') if alignment_note else None,
            "dice_registered": alignment_note.get('dice_registered') if alignment_note else None,
        })

    except Exception as e:
        logger.error(f"Fehler beim Ground-Truth-Vergleich: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({"success": False, "error": f"Internal error: {e}"}), 500

    finally:
        shutil.rmtree(temp_comparison_dir, ignore_errors=True)
