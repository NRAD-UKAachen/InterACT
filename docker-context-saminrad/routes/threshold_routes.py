# -*- coding: utf-8 -*-
"""
Threshold-Segmentierungs-Routes für RADSAM.
Enthält Routes für die Threshold-basierte Segmentierung.
"""

import os
import logging
import traceback
import numpy as np
import pydicom
import nibabel as nib
from PIL import Image
from flask import request, jsonify
from scipy.ndimage import zoom

from . import threshold_bp
from services.image_processing import get_slice_from_volume, apply_windowing, save_cutout_image, blend_mask_overlay
from services.archive_service import create_archive_folder, archive_results
from services.mask_processing import create_nifti_mask_from_arrays, post_process_nifti_mask, apply_threshold_refinement_to_mask, stacking_permuted_affine
from utils.helpers import log_mask_details
import state

logger = logging.getLogger(__name__)


@threshold_bp.route('/create_nifti_threshold', methods=['POST'])
def create_nifti_threshold_route():
    """
    Erstellt eine 3D-NIfTI-Maske basierend auf Threshold-Werten.
    """
    logger.info("--- Route /create_nifti_threshold gestartet ---")

    try:
        request_data = request.json
        lower_thresh = request_data.get('lower')
        upper_thresh = request_data.get('upper')
        do_fill_holes = request_data.get('fill_holes', False)
        do_remove_outliers = request_data.get('remove_outliers', False)
        min_outlier_size = int(request_data.get('min_outlier_size') or 100)
        current_ww = request_data.get('ww')
        current_wl = request_data.get('wl')
        view_axis = request_data.get('view_axis', 'axial')
        material_density = request_data.get('material_density')
        slice_start = request_data.get('slice_start')
        slice_end = request_data.get('slice_end')

        if material_density is not None:
            try:
                material_density = float(material_density)
                if material_density <= 0:
                    material_density = None
            except (ValueError, TypeError):
                material_density = None

        logger.debug(f"Parameter: lower={lower_thresh}, upper={upper_thresh}, "
                     f"fill_holes={do_fill_holes}, remove_outliers={do_remove_outliers}, "
                     f"min_size={min_outlier_size}, ww={current_ww}, wl={current_wl}, density={material_density}")

        if lower_thresh is None or upper_thresh is None:
            return jsonify({'success': False, 'error': 'Fehlende Threshold-Werte'}), 400

        try:
            lower_thresh, upper_thresh = float(lower_thresh), float(upper_thresh)
        except ValueError:
            return jsonify({'success': False, 'error': 'Ungültige Threshold-Werte'}), 400

        if lower_thresh > upper_thresh:
            lower_thresh, upper_thresh = upper_thresh, lower_thresh

        # Archive-Ordner vorbereiten
        archive_folder = create_archive_folder()
        results_folder = os.path.join(archive_folder, "segmentation_results")
        masks_folder = os.path.join(results_folder, "masks")
        overlays_folder = os.path.join(results_folder, "overlays")
        cutouts_folder = os.path.join(results_folder, "cutouts")
        os.makedirs(masks_folder, exist_ok=True)
        os.makedirs(overlays_folder, exist_ok=True)
        os.makedirs(cutouts_folder, exist_ok=True)

        mask_arrays_list = []
        ref_shape_for_empty = None
        affine_for_nifti = None
        header_for_nifti = None
        files_for_affine_calc = []

        # Slice-Range klemmen
        if state.CURRENT_FILE_TYPE == 'dicom_series':
            total_slices_for_range = len(state.DICOM_IMAGE_FILES)
        else:
            total_slices_for_range = state.SLICE_COUNTS.get(view_axis, 0)
        lo = int(slice_start) if slice_start is not None else 0
        hi = int(slice_end) if slice_end is not None else (total_slices_for_range - 1)
        if lo > hi:
            lo, hi = hi, lo
        lo = max(0, min(lo, total_slices_for_range - 1))
        hi = max(0, min(hi, total_slices_for_range - 1))

        if state.CURRENT_FILE_TYPE == 'dicom_series':
            mask_arrays_list, ref_shape_for_empty = _process_dicom_threshold(
                lower_thresh, upper_thresh, current_ww, current_wl,
                masks_folder, overlays_folder, cutouts_folder, lo, hi
            )
            files_for_affine_calc = [os.path.basename(p) for p in state.DICOM_IMAGE_FILES]

        elif state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            mask_arrays_list, ref_shape_for_empty = _process_volume_threshold(
                lower_thresh, upper_thresh, current_ww, current_wl, view_axis,
                masks_folder, overlays_folder, cutouts_folder, lo, hi
            )
            files_for_affine_calc = [state.CURRENT_ORIGINAL_FILENAME]
            # Affine passend zur Stapel-Achsenreihenfolge permutieren (siehe stacking_permuted_affine):
            # sonst wird bei anisotropem Spacing (z.B. 8 mm) das 3D-Modell entlang der falschen Achse gestreckt.
            affine_for_nifti = stacking_permuted_affine(state.CURRENT_VOLUME_AFFINE, view_axis)
            _perm_zooms = np.sqrt(np.sum(affine_for_nifti[:3, :3] ** 2, axis=0))
            _perm_zooms[~np.isfinite(_perm_zooms) | (_perm_zooms <= 1e-6)] = 1.0
            header_for_nifti = nib.Nifti1Header()
            header_for_nifti.set_data_dtype(np.uint8)
            header_for_nifti.set_data_shape(state.CURRENT_VOLUME_DATA.shape)  # ndim=3, sonst crasht set_zooms
            header_for_nifti.set_zooms(tuple(float(z) for z in _perm_zooms[:3]))

        else:
            return jsonify({'success': False, 'error': 'Kein gültiger Dateityp geladen'}), 400

        if not mask_arrays_list:
            return jsonify({'success': False, 'error': 'Keine Masken erstellt'}), 500

        # NIfTI-Ordner vorbereiten
        archive_nifti_folder = os.path.join(results_folder, "3d_models")
        os.makedirs(archive_nifti_folder, exist_ok=True)

        # Analyse-Maske erstellen
        logger.info("Erstelle 'Analyse-Maske' NIfTI (Threshold)...")
        success_analysis, analysis_nifti_filename, message, volume_cm3, weight_g = create_nifti_mask_from_arrays(
            files_for_affine_calc, mask_arrays_list, archive_nifti_folder,
            nifti_file_suffix=f"_{state.CURRENT_ORIGINAL_FILENAME}_{view_axis}_thresh_analysis_mask.nii.gz",
            input_affine=affine_for_nifti,
            input_header=header_for_nifti,
            downsample_factor=state.ANALYSIS_MASK_DOWNSAMPLE_FACTOR,
            material_density=material_density
        )

        if not success_analysis:
            return jsonify({'success': False, 'error': f'Fehler Erstellung Analyse-Maske: {message}'}), 500

        analysis_nifti_path = os.path.join(archive_nifti_folder, analysis_nifti_filename)

        # Nachbearbeitung
        if do_fill_holes or do_remove_outliers:
            logger.info("Starte Nachbearbeitung der Threshold-Maske...")
            post_success, post_message = post_process_nifti_mask(
                analysis_nifti_path, analysis_nifti_path,
                fill_holes=do_fill_holes, remove_outliers=do_remove_outliers, min_outlier_size=min_outlier_size
            )
            message += f" | Nachbearb.: {post_message}"

        # Viewer-Maske erstellen
        viewer_nifti_filename = f"viewer_{analysis_nifti_filename}"
        viewer_nifti_path = os.path.join(archive_nifti_folder, viewer_nifti_filename)
        nifti_filename_to_serve = _create_viewer_mask_threshold(
            analysis_nifti_path, viewer_nifti_path, analysis_nifti_filename
        )

        # URL erstellen
        nifti_url = f"/nifti_masks_thresh/{nifti_filename_to_serve}" if nifti_filename_to_serve else None

        # Segmentierungsparameter speichern
        segmentation_params = {
            "method": "threshold",
            "parameters": {
                "lower_threshold": lower_thresh,
                "upper_threshold": upper_thresh,
                "fill_holes": do_fill_holes,
                "remove_outliers": do_remove_outliers,
                "min_outlier_size": min_outlier_size,
                "window_width": current_ww,
                "window_level": current_wl,
                "material_density": material_density,
                "view_axis": view_axis
            },
            "output_files": {
                "analysis_nifti": analysis_nifti_filename,
                "viewer_nifti": viewer_nifti_filename
            },
            "volume_analysis": {
                "volume_cm3": volume_cm3,
                "weight_g": weight_g,
                "material_density_g_cm3": material_density
            }
        }

        archive_results(segmentation_params=segmentation_params)

        # Overlay-Liste erstellen
        final_overlay_files = []
        if os.path.isdir(overlays_folder):
            final_overlay_files = sorted([f for f in os.listdir(overlays_folder) if f.endswith('_overlay.png')])

        response_data = {
            "success": True,
            "message": message,
            "nifti_filename": nifti_filename_to_serve,
            "nifti_url": nifti_url,
            "method": "threshold",
            "final_overlay_files": final_overlay_files
        }

        if volume_cm3 is not None:
            response_data["volume_cm3"] = volume_cm3
        if weight_g is not None:
            response_data["weight_g"] = weight_g
        if material_density is not None:
            response_data["material_density"] = material_density

        logger.info(f"Threshold-Maske erfolgreich erstellt: {nifti_filename_to_serve}")
        return jsonify(response_data)

    except Exception as e:
        logger.error(f"Fehler bei der Threshold-Segmentierung: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500


def _process_dicom_threshold(lower_thresh, upper_thresh, ww, wl, masks_folder, overlays_folder, cutouts_folder, lo=None, hi=None):
    """Verarbeitet DICOM-Serie mit Threshold. Slices außerhalb [lo, hi] werden auf Null gesetzt."""
    mask_arrays = []
    ref_shape = None
    total = len(state.DICOM_IMAGE_FILES)
    if lo is None:
        lo = 0
    if hi is None:
        hi = total - 1

    for i, dicom_filepath in enumerate(state.DICOM_IMAGE_FILES):
        try:
            ds = pydicom.dcmread(dicom_filepath, force=True)
            pixel_array = ds.pixel_array.astype(np.float32)
            slope = float(getattr(ds, 'RescaleSlope', 1))
            intercept = float(getattr(ds, 'RescaleIntercept', 0))

            raw_data = pixel_array * slope + intercept if slope != 1 or intercept != 0 else pixel_array

            if i < lo or i > hi:
                if ref_shape is None:
                    ref_shape = raw_data.shape
                mask_arrays.append(np.zeros(ref_shape, dtype=np.uint8))
                continue

            mask_bool = (raw_data >= lower_thresh) & (raw_data <= upper_thresh)
            mask_uint8 = mask_bool.astype(np.uint8)

            if ref_shape is None:
                ref_shape = mask_uint8.shape

            # Maske und Overlay speichern
            base_name = os.path.splitext(os.path.basename(dicom_filepath))[0]

            # Maske speichern
            mask_filename = f"{base_name}_mask.png"
            Image.fromarray(mask_uint8 * 255, 'L').save(os.path.join(masks_folder, mask_filename))

            # Overlay erstellen
            slice_np_rgb, _, _, _, _ = apply_windowing(raw_data, ww, wl, ds)
            overlay_rgb = blend_mask_overlay(slice_np_rgb, mask_uint8 == 1, color=(0, 255, 0))  # Grün für Threshold

            overlay_filename = f"{base_name}_overlay.png"
            Image.fromarray(overlay_rgb, 'RGB').save(os.path.join(overlays_folder, overlay_filename))

            # Cutout speichern
            cutout_filename = f"{base_name}_cutout.png"
            save_cutout_image(slice_np_rgb, mask_uint8, os.path.join(cutouts_folder, cutout_filename))

            mask_arrays.append(mask_uint8 if mask_uint8.shape == ref_shape else np.zeros(ref_shape, dtype=np.uint8))

        except Exception as e:
            logger.warning(f"Fehler bei Verarbeitung von DICOM-Slice {i}: {e}")
            if ref_shape:
                mask_arrays.append(np.zeros(ref_shape, dtype=np.uint8))

    return mask_arrays, ref_shape


def _process_volume_threshold(lower_thresh, upper_thresh, ww, wl, axis, masks_folder, overlays_folder, cutouts_folder, lo=None, hi=None):
    """Verarbeitet volumetrische Daten mit Threshold. Slices außerhalb [lo, hi] werden auf Null gesetzt."""
    mask_arrays = []
    ref_shape = None
    num_slices = state.SLICE_COUNTS.get(axis, 0)
    if lo is None:
        lo = 0
    if hi is None:
        hi = num_slices - 1

    for i in range(num_slices):
        try:
            slice_2d = get_slice_from_volume(state.CURRENT_VOLUME_DATA, i, axis)
            if slice_2d is None:
                if ref_shape:
                    mask_arrays.append(np.zeros(ref_shape, dtype=np.uint8))
                continue

            if i < lo or i > hi:
                if ref_shape is None:
                    ref_shape = slice_2d.shape
                mask_arrays.append(np.zeros(ref_shape, dtype=np.uint8))
                continue

            mask_bool = (slice_2d >= lower_thresh) & (slice_2d <= upper_thresh)
            mask_uint8 = mask_bool.astype(np.uint8)

            if ref_shape is None:
                ref_shape = mask_uint8.shape

            # Maske und Overlay speichern
            base_name = f"{state.CURRENT_ORIGINAL_FILENAME}_{axis}_{i:03d}"

            # Maske speichern
            mask_filename = f"{base_name}_mask.png"
            Image.fromarray(mask_uint8 * 255, 'L').save(os.path.join(masks_folder, mask_filename))

            # Overlay erstellen
            slice_np_rgb, _, _, _, _ = apply_windowing(slice_2d, ww, wl)
            overlay_rgb = blend_mask_overlay(slice_np_rgb, mask_uint8 == 1, color=(0, 255, 0))

            overlay_filename = f"{base_name}_overlay.png"
            Image.fromarray(overlay_rgb, 'RGB').save(os.path.join(overlays_folder, overlay_filename))

            # Cutout speichern
            cutout_filename = f"{base_name}_cutout.png"
            save_cutout_image(slice_np_rgb, mask_uint8, os.path.join(cutouts_folder, cutout_filename))

            mask_arrays.append(mask_uint8 if mask_uint8.shape == ref_shape else np.zeros(ref_shape, dtype=np.uint8))

        except Exception as e:
            logger.warning(f"Fehler bei Verarbeitung von Slice {i}: {e}")
            if ref_shape:
                mask_arrays.append(np.zeros(ref_shape, dtype=np.uint8))

    return mask_arrays, ref_shape


def _create_viewer_mask_threshold(analysis_nifti_path, viewer_nifti_path, analysis_nifti_filename):
    """Erstellt eine gedownsampelte Viewer-Maske für Threshold."""
    try:
        img_for_viewer = nib.load(analysis_nifti_path)
        data_for_viewer = img_for_viewer.get_fdata().astype(np.uint8)
        affine_for_viewer = img_for_viewer.affine.copy()
        header_for_viewer = img_for_viewer.header.copy()

        downsampled_data = zoom(
            data_for_viewer, state.VIEWER_SPECIFIC_DOWNSAMPLE_FACTOR, order=0, mode='nearest'
        ).astype(np.uint8)

        new_affine = affine_for_viewer.copy()
        original_zooms = np.sqrt(np.sum(new_affine[:3, :3] ** 2, axis=0))
        original_zooms[~np.isfinite(original_zooms) | (original_zooms <= 1e-6)] = 1.0

        for i in range(3):
            new_affine[:3, i] = new_affine[:3, i] / state.VIEWER_SPECIFIC_DOWNSAMPLE_FACTOR

        header_for_viewer.set_data_dtype(np.uint8)
        header_for_viewer.set_data_shape(downsampled_data.shape)
        new_zooms = original_zooms / state.VIEWER_SPECIFIC_DOWNSAMPLE_FACTOR
        header_for_viewer.set_zooms(new_zooms[:3])

        nifti_img = nib.Nifti1Image(downsampled_data, new_affine, header=header_for_viewer)
        nifti_img.set_sform(new_affine, code=1)
        nifti_img.set_qform(new_affine, code=1)
        nib.save(nifti_img, viewer_nifti_path)

        logger.info(f"✅ Viewer NIfTI Maske Threshold gespeichert: {viewer_nifti_path}")
        return os.path.basename(viewer_nifti_path)

    except Exception as e:
        logger.error(f"Fehler beim Erstellen der Viewer-Maske: {e}")
        return analysis_nifti_filename


def _resolve_slice_info_from_mask_name(mask_filename, default_axis):
    """
    Leitet (slice_index, axis) aus einem Masken-Dateinamen ab.
    NRRD/NIfTI: <base>_<axis>_<idx>_mask.png
    DICOM: <dicom_basename>_mask.png → Mapping über state.DICOM_IMAGE_FILES.
    """
    base = os.path.splitext(mask_filename)[0]
    if base.endswith('_mask'):
        base = base[:-5]

    if state.CURRENT_FILE_TYPE in ('nrrd', 'nifti'):
        parts = base.split('_')
        if len(parts) >= 2 and parts[-1].isdigit() and parts[-2] in ('axial', 'sagittal', 'coronal'):
            return int(parts[-1]), parts[-2]
        for i, p in enumerate(parts):
            if p in ('axial', 'sagittal', 'coronal') and i + 1 < len(parts) and parts[i + 1].isdigit():
                return int(parts[i + 1]), p
        return None, default_axis

    for idx, dicom_path in enumerate(state.DICOM_IMAGE_FILES):
        if os.path.splitext(os.path.basename(dicom_path))[0] == base:
            return idx, 'axial'
    return None, default_axis


def _regenerate_overlay_purple(mask_refined, slice_idx, axis, ww, wl, overlay_path):
    """
    Erzeugt ein lila Overlay-PNG aus verfeinerter Maske und Rohdaten des Slices.
    """
    try:
        ds_meta = None
        if state.CURRENT_FILE_TYPE == 'dicom_series':
            if not (0 <= slice_idx < len(state.DICOM_IMAGE_FILES)):
                return False
            ds_meta = pydicom.dcmread(state.DICOM_IMAGE_FILES[slice_idx], force=True)
            raw = ds_meta.pixel_array.astype(np.float32)
            slope = float(getattr(ds_meta, 'RescaleSlope', 1))
            intercept = float(getattr(ds_meta, 'RescaleIntercept', 0))
            raw = raw * slope + intercept
        else:
            raw = get_slice_from_volume(state.CURRENT_VOLUME_DATA, slice_idx, axis)
            if raw is None:
                return False

        slice_rgb, _, _, _, _ = apply_windowing(raw, ww, wl, ds_meta)
        overlay_img = Image.fromarray(slice_rgb)
        mask_pil = Image.fromarray(mask_refined * 255, 'L').resize(overlay_img.size, Image.Resampling.NEAREST)

        overlay_rgb = blend_mask_overlay(np.array(overlay_img), np.array(mask_pil) > 0, color=(128, 0, 255))
        Image.fromarray(overlay_rgb, 'RGB').save(overlay_path)
        return True
    except Exception as e:
        logger.warning(f"Konnte Overlay nicht lila regenerieren ({overlay_path}): {e}")
        return False


def _refine_folder(masks_folder, overlays_folder, default_axis, threshold_lower, threshold_upper, ww, wl):
    """
    Iteriert über alle Masken in einem Ordner, wendet Threshold-AND an,
    überschreibt Masken-PNG und erzeugt Overlay lila neu.
    """
    if not os.path.isdir(masks_folder):
        return 0

    os.makedirs(overlays_folder, exist_ok=True)
    count = 0
    for mask_filename in sorted(os.listdir(masks_folder)):
        if not mask_filename.endswith('_mask.png'):
            continue
        mask_path = os.path.join(masks_folder, mask_filename)
        slice_idx, axis = _resolve_slice_info_from_mask_name(mask_filename, default_axis)
        if slice_idx is None:
            logger.debug(f"Kein Slice-Mapping für {mask_filename}, überspringe")
            continue

        try:
            mask_arr = (np.array(Image.open(mask_path).convert('L')) > 128).astype(np.uint8)
        except Exception as e:
            logger.warning(f"Konnte Maske {mask_filename} nicht laden: {e}")
            continue

        refined = apply_threshold_refinement_to_mask(mask_arr, slice_idx, axis, threshold_lower, threshold_upper)
        if refined is None:
            continue

        Image.fromarray(refined * 255, mode='L').save(mask_path)

        overlay_filename = mask_filename.replace('_mask.png', '_overlay.png')
        overlay_path = os.path.join(overlays_folder, overlay_filename)
        if _regenerate_overlay_purple(refined, slice_idx, axis, ww, wl, overlay_path):
            count += 1

    return count


@threshold_bp.route('/refine_sam_with_threshold', methods=['POST'])
def refine_sam_with_threshold():
    """
    Verfeinert bestehende SAM-Masken mit Threshold-Werten.
    Überschreibt gespeicherte Masken und ersetzt rote SAM-Overlays durch lila verfeinerte Overlays.
    """
    try:
        data = request.json
        threshold_lower = data.get('threshold_lower')
        threshold_upper = data.get('threshold_upper')
        ww = data.get('ww')
        wl = data.get('wl')
        default_axis = data.get('axis', 'axial')

        if threshold_lower is None or threshold_upper is None:
            return jsonify({'success': False, 'error': 'Threshold-Werte fehlen'}), 400

        threshold_lower = float(threshold_lower)
        threshold_upper = float(threshold_upper)
        if threshold_lower > threshold_upper:
            threshold_lower, threshold_upper = threshold_upper, threshold_lower

        archive_folder = create_archive_folder()
        results_folder = os.path.join(archive_folder, "segmentation_results")
        os.makedirs(results_folder, exist_ok=True)

        # Alle relevanten Masken-/Overlay-Ordner lila neu schreiben
        folder_pairs = [
            ('masks', 'overlays'),
            ('masks_few_shot', 'overlays_few_shot'),
            ('masks_s1sfq', 'overlays_s1sfq'),
            ('masks_memory', 'overlays_memory'),
        ]
        total = 0
        for masks_sub, overlays_sub in folder_pairs:
            total += _refine_folder(
                os.path.join(results_folder, masks_sub),
                os.path.join(results_folder, overlays_sub),
                default_axis, threshold_lower, threshold_upper, ww, wl
            )

        # Parameter speichern
        hybrid_params = {
            "hybrid_refinement": {
                "threshold_lower": threshold_lower,
                "threshold_upper": threshold_upper,
                "applied": True,
                "window_width": ww,
                "window_level": wl,
                "overlays_rewritten": total
            }
        }

        import json
        params_file = os.path.join(results_folder, "segmentation_parameters.json")
        if os.path.exists(params_file):
            with open(params_file, 'r', encoding='utf-8') as f:
                existing_params = json.load(f)
            existing_params.update(hybrid_params)
            with open(params_file, 'w', encoding='utf-8') as f:
                json.dump(existing_params, f, indent=4, ensure_ascii=False)
        else:
            with open(params_file, 'w', encoding='utf-8') as f:
                json.dump(hybrid_params, f, indent=4, ensure_ascii=False)

        logger.info(f"Hybrid-Refinement angewendet: {threshold_lower}–{threshold_upper}, "
                    f"{total} Overlay(s) lila neu geschrieben")

        return jsonify({
            'success': True,
            'message': f'Threshold-Verfeinerung angewendet ({total} Overlay(s) aktualisiert)'
        })

    except Exception as e:
        logger.error(f"Fehler bei der Threshold-Verfeinerung: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500


@threshold_bp.route('/get_hybrid_params')
def get_hybrid_params():
    """
    Gibt die aktuellen Hybrid-Parameter zurück (falls vorhanden).
    """
    try:
        archive_folder = create_archive_folder()
        results_folder = os.path.join(archive_folder, "segmentation_results")
        params_file = os.path.join(results_folder, "segmentation_parameters.json")

        if os.path.exists(params_file):
            import json
            with open(params_file, 'r', encoding='utf-8') as f:
                params = json.load(f)

            if 'hybrid_refinement' in params:
                return jsonify({
                    'success': True,
                    'hybrid_params': params['hybrid_refinement']
                })

        return jsonify({'success': True, 'hybrid_params': None})

    except Exception as e:
        logger.error(f"Fehler beim Abrufen der Hybrid-Parameter: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
