# -*- coding: utf-8 -*-
"""
Maskenverarbeitungs-Service für RADSAM.
Enthält Funktionen zum Erstellen und Nachbearbeiten von 3D-Masken.
"""

import os
import logging
import traceback
import numpy as np
import nibabel as nib
import pydicom
from PIL import Image
from scipy import ndimage as ndi
from scipy.ndimage import zoom, binary_erosion

from utils.helpers import log_mask_details

logger = logging.getLogger(__name__)


def stacking_permuted_affine(affine, segmentation_axis):
    """
    Die 3D-Analyse-Maske entsteht durch np.stack(2D-Slices, axis=-1). Ihre Achsen-
    reihenfolge ist deshalb (In-Plane-Achse-1, In-Plane-Achse-2, Slice-Achse) und NICHT
    die des Original-Volumens. Die Original-Affine muss spaltenweise identisch permutiert
    werden – sonst landet bei anisotropem Spacing (z.B. 8 mm Schichtdicke) der Spacing-
    Wert auf der falschen Achse und das 3D-Modell wird um diesen Faktor gestreckt
    (Bug z.B. bei Tier12_rechts_1, Original 1×1×8 mm). Bei isotropen Volumen ODER
    axialer Segmentierung ist die Permutation die Identität (No-Op).
    """
    import state
    s = state.AXIS_INDEX_FOR_SLICING.get(segmentation_axis, 2)
    in_plane = [a for a in (0, 1, 2) if a != s]   # aufsteigend -> (H, W) der 2D-Slices
    perm = [in_plane[0], in_plane[1], s]          # gestapelte Achse k -> Original-Achse perm[k]
    aff = np.asarray(affine, dtype=float)
    out = np.eye(4)
    for k in range(3):
        out[:3, k] = aff[:3, perm[k]]
    out[:3, 3] = aff[:3, 3]
    return out


def extract_surface(mask):
    """
    Extrahiert die Oberfläche einer binären 3D-Maske.

    Args:
        mask: 3D binäre Maske als NumPy-Array

    Returns:
        np.ndarray: Array mit Koordinaten der Oberflächenvoxel
    """
    if mask.sum() == 0:
        return np.array([])
    # Die Oberfläche sind alle Voxel, die nach einer Erosion verschwinden.
    eroded_mask = binary_erosion(mask, structure=np.ones((3, 3, 3)), border_value=0)
    surface = mask ^ eroded_mask  # XOR-Operation
    return np.array(np.where(surface)).T


def apply_threshold_refinement_to_mask(mask_np, slice_index, axis, threshold_lower, threshold_upper):
    """
    Wendet Threshold-Verfeinerung auf eine Maske an.

    Args:
        mask_np: 2D Maske als NumPy-Array
        slice_index: Index des Slices
        axis: Achsenname ('axial', 'sagittal', 'coronal')
        threshold_lower: Unterer Threshold-Wert
        threshold_upper: Oberer Threshold-Wert

    Returns:
        np.ndarray: Verfeinerte Maske oder None bei Fehler
    """
    import state
    from services.image_processing import get_slice_from_volume

    try:
        # Hole die Rohdaten des Slices
        raw_slice = None
        if state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            raw_slice = get_slice_from_volume(state.CURRENT_VOLUME_DATA, slice_index, axis)
        elif state.CURRENT_FILE_TYPE == 'dicom_series':
            if 0 <= slice_index < len(state.DICOM_IMAGE_FILES):
                ds = pydicom.dcmread(state.DICOM_IMAGE_FILES[slice_index], force=True)
                pixel_array = ds.pixel_array.astype(np.float32)
                slope = float(getattr(ds, 'RescaleSlope', 1))
                intercept = float(getattr(ds, 'RescaleIntercept', 0))
                raw_slice = pixel_array * slope + intercept

        if raw_slice is None:
            logger.warning(f"Konnte Rohdaten für Slice {slice_index} nicht laden")
            return None

        # Erstelle Threshold-Maske
        threshold_mask = ((raw_slice >= threshold_lower) & (raw_slice <= threshold_upper)).astype(np.uint8)

        # Kombiniere mit der Original-Maske (AND-Verknüpfung)
        # Skaliere die Masken auf gleiche Größe falls nötig
        if mask_np.shape != threshold_mask.shape:
            mask_pil = Image.fromarray(mask_np * 255, 'L')
            mask_pil_resized = mask_pil.resize((threshold_mask.shape[1], threshold_mask.shape[0]), Image.Resampling.NEAREST)
            mask_np_resized = (np.array(mask_pil_resized) > 128).astype(np.uint8)
        else:
            mask_np_resized = mask_np

        refined_mask = mask_np_resized & threshold_mask

        return refined_mask

    except Exception as e:
        logger.error(f"Fehler bei der Threshold-Verfeinerung für Slice {slice_index}: {e}")
        return None


def create_nifti_mask_from_arrays(
        reference_identifiers,
        mask_data,
        output_nifti_folder,
        nifti_file_suffix="_3Dmask.nii.gz",
        input_affine=None,
        input_header=None,
        downsample_factor=None,
        material_density=None
):
    """
    Erstellt eine 3D NIfTI-Maske aus 2D-Masken-Arrays oder einem 3D-Array.

    Args:
        reference_identifiers: Liste von Dateinamen/Identifiern für die Benennung
        mask_data: Liste von 2D NumPy-Arrays oder ein 3D NumPy-Array
        output_nifti_folder: Ausgabeordner für die NIfTI-Datei
        nifti_file_suffix: Suffix für den Dateinamen
        input_affine: Optionale Affine-Matrix
        input_header: Optionaler NIfTI-Header
        downsample_factor: Optionaler Downsampling-Faktor (0 < factor < 1)
        material_density: Optionale Materialdichte für Gewichtsberechnung (g/cm³)

    Returns:
        tuple: (success, filename, message, volume_cm3, weight_g)
    """
    import state

    is_3d_input = isinstance(mask_data, np.ndarray) and mask_data.ndim == 3
    final_mask_data_3d_uint8 = None

    if not reference_identifiers or (not is_3d_input and not isinstance(mask_data, list)):
        logger.error("[NIfTI Gen] Ungültige Eingabe: Referenz oder Maskendaten fehlen.")
        return False, None, "Ungültige Eingabe: Referenz oder Maskendaten fehlen.", None, None

    if is_3d_input:
        if mask_data.dtype != np.uint8:
            logger.debug(f"[NIfTI Gen] Konvertiere 3D Maskendaten von {mask_data.dtype} zu uint8.")
            final_mask_data_3d_uint8 = mask_data.astype(np.uint8)
        else:
            final_mask_data_3d_uint8 = mask_data
    else:
        mask_arrays_list = mask_data
        if not mask_arrays_list:
            logger.warning("[NIfTI Gen] Leere 2D-Masken-Liste erhalten.")
            return False, None, "Leere 2D-Masken-Liste.", None, None

        ref_shape_2d_list = next((arr.shape for arr in mask_arrays_list if arr is not None and isinstance(arr, np.ndarray) and arr.ndim == 2), None)

        if ref_shape_2d_list is None:
            if state.CURRENT_FILE_TYPE == 'dicom_series' and state.DICOM_IMAGE_FILES:
                try:
                    temp_ds = pydicom.dcmread(state.DICOM_IMAGE_FILES[0])
                    ref_shape_2d_list = temp_ds.pixel_array.shape
                    logger.info(f"[NIfTI Gen] Referenz-Shape für leere Masken aus DICOM geladen: {ref_shape_2d_list}")
                except Exception as e:
                    logger.error(f"[NIfTI Gen] Konnte Ref-Shape aus erstem DICOM nicht laden: {e}")
                    return False, None, f"Konnte Ref-Shape aus erstem DICOM nicht laden: {e}", None, None
            else:
                logger.error("[NIfTI Gen] Keine Referenz-Shape für 2D-Masken-Stapelung.")
                return False, None, "Keine Referenz-Shape für 2D-Masken-Stapelung.", None, None

        processed_mask_arrays = []
        for arr in mask_arrays_list:
            if arr is not None and isinstance(arr, np.ndarray) and arr.shape == ref_shape_2d_list:
                if arr.dtype != np.uint8:
                    processed_mask_arrays.append(arr.astype(np.uint8))
                else:
                    processed_mask_arrays.append(arr)
            else:
                processed_mask_arrays.append(np.zeros(ref_shape_2d_list, dtype=np.uint8))

        if not processed_mask_arrays:
            logger.warning("[NIfTI Gen] Keine verarbeitbaren 2D-Masken nach Filterung.")
            return False, None, "Keine verarbeitbaren 2D-Masken.", None, None
        stacked_3d_hwd = np.stack(processed_mask_arrays, axis=-1)
        final_mask_data_3d_uint8 = stacked_3d_hwd

    if final_mask_data_3d_uint8 is None:
        logger.error("[NIfTI Gen] Konnte finale 3D uint8 Maskendaten nicht erstellen.")
        return False, None, "Konnte finale 3D uint8 Maskendaten nicht erstellen.", None, None

    logger.info(f"[NIfTI Gen] Ursprüngliche Masken-Shape (vor Downsampling): {final_mask_data_3d_uint8.shape}, Datentyp: {final_mask_data_3d_uint8.dtype}")

    # Affine-Matrix bestimmen
    current_affine = None
    if input_affine is not None and np.all(np.isfinite(input_affine)):
        current_affine = np.copy(input_affine)
        logger.debug("[NIfTI Gen] Vorgegebene Affine wird verwendet.")
    elif state.CURRENT_FILE_TYPE == 'dicom_series' and state.DICOM_IMAGE_FILES:
        logger.info("[NIfTI Gen] Berechne Affine aus DICOM-Serie.")
        current_affine = _compute_dicom_affine()
    else:
        logger.warning("[NIfTI Gen] input_affine nicht gegeben und kein DICOM-Fall. Standard-Affine [1,1,1,1] wird verwendet.")
        current_affine = np.diag([1., 1., 1., 1.])

    # Header erstellen
    current_header = nib.Nifti1Header()
    if input_header is not None and isinstance(input_header, nib.Nifti1Header):
        current_header = input_header.copy()

    current_header.set_data_dtype(np.uint8)
    current_header.set_data_shape(final_mask_data_3d_uint8.shape)

    original_zooms = np.sqrt(np.sum(current_affine[:3, :3] ** 2, axis=0))
    original_zooms[~np.isfinite(original_zooms) | (original_zooms <= 1e-6)] = 1.0
    current_header.set_zooms(original_zooms[:3])

    data_to_save = final_mask_data_3d_uint8
    affine_to_save = current_affine.copy()
    header_to_save = current_header.copy()

    # Downsampling anwenden
    if downsample_factor is not None and 0 < downsample_factor < 1:
        logger.info(f"[NIfTI Gen] Downsampling Maske mit Faktor: {downsample_factor} für Datei-Erstellung.")
        data_to_save = zoom(final_mask_data_3d_uint8, downsample_factor, order=0, mode='nearest').astype(np.uint8)

        for i in range(3):
            affine_to_save[:3, i] = affine_to_save[:3, i] / downsample_factor

        new_zooms = original_zooms / downsample_factor
        header_to_save.set_zooms(new_zooms[:3])
        header_to_save.set_data_shape(data_to_save.shape)
        logger.info(f"[NIfTI Gen] Downsampled Masken-Shape für Speichern: {data_to_save.shape}, Neue Zooms: {new_zooms[:3]}")

    final_mask_data_3d_contiguous = np.ascontiguousarray(data_to_save)

    log_mask_details(final_mask_data_3d_contiguous,
                     f"Maskendaten in create_nifti_mask_from_arrays (Shape: {final_mask_data_3d_contiguous.shape}, Dtype: {final_mask_data_3d_contiguous.dtype})")

    # Volumen- und Gewichtsberechnung
    volume_cm3, weight_g = _calculate_volume_and_weight(
        final_mask_data_3d_contiguous, affine_to_save, header_to_save, material_density
    )

    # NIfTI-Objekt erstellen und speichern
    try:
        nifti_img = nib.Nifti1Image(final_mask_data_3d_contiguous, affine_to_save, header=header_to_save)
        nifti_img.set_sform(affine_to_save, code=1)
        nifti_img.set_qform(affine_to_save, code=1)
        logger.debug(f"   [NIfTI Gen] Nifti1Image Objekt erstellt. Affine im Objekt:\n{nifti_img.affine}")
    except Exception as e_nii:
        logger.error(f"[NIfTI Gen] Fehler beim Erstellen des NIfTI-Objekts: {e_nii}\n{traceback.format_exc()}")
        return False, None, f"Fehler NIfTI Objekt: {e_nii}", None, None

    # Dateiname generieren
    nifti_filename_base = os.path.splitext(reference_identifiers[0])[0]
    if nifti_filename_base.lower().endswith(".nii"):
        nifti_filename_base = os.path.splitext(nifti_filename_base)[0]

    if nifti_file_suffix.startswith("_") or nifti_filename_base.endswith("_"):
        nifti_filename = f"{nifti_filename_base}{nifti_file_suffix}"
    else:
        nifti_filename = f"{nifti_filename_base}_{nifti_file_suffix}"

    output_nifti_path = os.path.join(output_nifti_folder, nifti_filename)

    try:
        logger.info(f"Versuche NIfTI-Datei zu speichern unter: {output_nifti_path}")
        nib.save(nifti_img, output_nifti_path)
        logger.info(f"✅ 3D NIfTI Maske erfolgreich gespeichert: {output_nifti_path}")

        result_message = "NIfTI erfolgreich erstellt."
        if volume_cm3 is not None:
            result_message += f" Volumen: {volume_cm3:.2f} cm³"
            if weight_g is not None:
                result_message += f", Gewicht: {weight_g:.2f} g"

        return True, nifti_filename, result_message, volume_cm3, weight_g
    except Exception as e_save:
        logger.error(f"!!! FEHLER beim Speichern der NIfTI-Datei: {e_save}\n{traceback.format_exc()}")
        return False, None, f"Fehler Speichern NIfTI: {e_save}", None, None


def _compute_dicom_affine():
    """Berechnet die Affine-Matrix aus einer DICOM-Serie."""
    import state

    try:
        first_ds = pydicom.dcmread(state.DICOM_IMAGE_FILES[0])
        ipp_first = np.array(first_ds.ImagePositionPatient, dtype=float)
        iop_val = first_ds.ImageOrientationPatient
        iop = np.array(iop_val, dtype=float) if iop_val else np.array([1, 0, 0, 0, 1, 0], dtype=float)

        ps_val = first_ds.PixelSpacing
        pixel_spacing = np.array(ps_val, dtype=float) if ps_val else np.array([1, 1], dtype=float)

        row_vec = iop[0:3] * pixel_spacing[1]
        col_vec = iop[3:6] * pixel_spacing[0]

        st_val = getattr(first_ds, 'SliceThickness', None)
        slice_thickness = float(st_val) if st_val is not None else 1.0

        slice_vec_dir = np.cross(col_vec, row_vec)

        if len(state.DICOM_IMAGE_FILES) > 1:
            last_ds = pydicom.dcmread(state.DICOM_IMAGE_FILES[-1])
            ipp_last_val = getattr(last_ds, 'ImagePositionPatient', None)
            if ipp_last_val:
                ipp_last = np.array(ipp_last_val, dtype=float)
                calculated_slice_vec_dir = (ipp_last - ipp_first) / (len(state.DICOM_IMAGE_FILES) - 1)
                if np.linalg.norm(calculated_slice_vec_dir) > 1e-5:
                    slice_vec_dir = calculated_slice_vec_dir

        actual_slice_distance = np.linalg.norm(slice_vec_dir)
        if not (actual_slice_distance > 1e-6 and len(state.DICOM_IMAGE_FILES) > 1):
            norm_dir = slice_vec_dir / np.linalg.norm(slice_vec_dir) if np.linalg.norm(slice_vec_dir) > 1e-6 else np.array([0, 0, 1])
            slice_vec = norm_dir * slice_thickness
        else:
            slice_vec = slice_vec_dir

        current_affine = np.eye(4)
        current_affine[:3, 0] = col_vec
        current_affine[:3, 1] = row_vec
        current_affine[:3, 2] = slice_vec
        current_affine[:3, 3] = ipp_first

        if not np.all(np.isfinite(current_affine)):
            logger.warning(f"[NIfTI Gen] Berechnete DICOM Affine nicht endlich. Fallback auf Standard-Spacing.")
            current_affine = np.diag([pixel_spacing[0], pixel_spacing[1], slice_thickness, 1.0])
            current_affine[:3, 3] = ipp_first
            if not np.all(np.isfinite(current_affine)):
                current_affine = np.diag([1., 1., 1., 1.])

        return current_affine

    except Exception as e:
        logger.error(f"[NIfTI Gen] FEHLER bei DICOM Affine Berechnung: {e}. Standard-Affine [1,1,1,1] wird verwendet.")
        return np.diag([1., 1., 1., 1.])


def _calculate_volume_and_weight(mask_data, affine, header, material_density):
    """Berechnet Volumen und Gewicht einer Maske."""
    volume_cm3 = None
    weight_g = None

    try:
        voxel_volume_mm3 = abs(np.linalg.det(affine[:3, :3]))

        if not np.isfinite(voxel_volume_mm3) or voxel_volume_mm3 < 1e-9:
            logger.warning(f"[NIfTI Gen] Ungültiger Determinant ({voxel_volume_mm3}) aus Affine-Matrix. Fallback auf Voxel-Spacing aus Header.")
            zooms = header.get_zooms()[:3]
            if np.all(np.isfinite(zooms)) and np.all(zooms > 1e-9):
                voxel_volume_mm3 = np.prod(zooms)
                logger.info(f"[NIfTI Gen] Fallback Voxel-Volumen aus Header-Zooms erfolgreich: {voxel_volume_mm3:.6f} mm³")
            else:
                logger.error("[NIfTI Gen] Kritischer Fehler: Sowohl Affine als auch Header-Zooms sind ungültig.")
                return None, None

        total_voxels = np.sum(mask_data > 0)
        volume_mm3 = total_voxels * voxel_volume_mm3
        volume_cm3 = volume_mm3 / 1000.0

        if material_density is not None and material_density > 0:
            weight_g = volume_cm3 * material_density

        logger.info(f"[NIfTI Gen] Volumenberechnung erfolgreich: {total_voxels} Voxel, "
                    f"Voxel-Volumen: {voxel_volume_mm3:.6f} mm³, "
                    f"Gesamtvolumen: {volume_cm3:.2f} cm³")
        if weight_g is not None:
            logger.info(f"[NIfTI Gen] Gewichtsberechnung erfolgreich: Dichte={material_density:.2f} g/cm³, Gewicht={weight_g:.2f} g")

    except Exception as e:
        logger.warning(f"[NIfTI Gen] Fehler bei der Volumen/Gewichtsberechnung: {e}")

    return volume_cm3, weight_g


def post_process_nifti_mask(nifti_input_path, nifti_output_path, fill_holes=False, remove_outliers=False, min_outlier_size=100):
    """
    Führt Nachbearbeitung auf einer NIfTI-Maske durch.

    Args:
        nifti_input_path: Pfad zur Eingabe-NIfTI-Datei
        nifti_output_path: Pfad zur Ausgabe-NIfTI-Datei
        fill_holes: Löcher in der Maske füllen
        remove_outliers: Kleine Objekte entfernen
        min_outlier_size: Minimale Größe in Voxeln für Objekte

    Returns:
        tuple: (success, message)
    """
    if not (fill_holes or remove_outliers):
        return True, "Keine Nachbearbeitung angefordert."

    logger.info(f"Starte Nachbearbeitung für: {nifti_input_path}")
    try:
        nifti_img = nib.load(nifti_input_path)
        data = nifti_img.get_fdata().astype(np.uint8)
        log_mask_details(data, f"Daten GELADEN in post_process_nifti_mask (aus {nifti_input_path})")

        processed_data = data.copy()
        original_affine, original_header = nifti_img.affine.copy(), nifti_img.header.copy()

        if original_header.get_data_dtype() != np.uint8:
            original_header.set_data_dtype(np.uint8)

        if fill_holes and processed_data.ndim >= 2:
            logger.info("[PostProc] Fülle Löcher...")
            processed_data = ndi.binary_fill_holes(processed_data).astype(np.uint8)

        if remove_outliers and processed_data.ndim == 3:
            logger.info(f"[PostProc] Entferne Ausreißer kleiner als {min_outlier_size} Voxel...")
            binary_for_labeling = (processed_data > 0).astype(int)
            labeled_array, num_features = ndi.label(binary_for_labeling, structure=np.ones((3, 3, 3)))
            logger.debug(f"[PostProc] {num_features} Objekte gefunden.")
            if num_features > 0:
                object_sizes = np.bincount(labeled_array.ravel())
                labels_to_keep = np.where((object_sizes >= min_outlier_size) & (np.arange(len(object_sizes)) != 0))[0]
                if len(labels_to_keep) > 0:
                    processed_data = np.isin(labeled_array, labels_to_keep).astype(np.uint8)
                elif num_features > 0:
                    logger.info(f"[PostProc] Alle {num_features} Objekte waren kleiner als {min_outlier_size}. Maske wird leer.")
                    processed_data = np.zeros_like(processed_data, dtype=np.uint8)

        log_mask_details(processed_data, f"Daten NACH Nachbearbeitung in post_process_nifti_mask (vor Speichern nach {nifti_output_path})")

        original_header.set_data_shape(processed_data.shape)
        original_header.set_data_dtype(np.uint8)

        processed_nifti_img = nib.Nifti1Image(processed_data, original_affine, header=original_header)
        nib.save(processed_nifti_img, nifti_output_path)
        logger.info(f"✅ Nachbearbeitete 3D NIfTI Maske gespeichert: {nifti_output_path}")
        return True, "Nachbearbeitung erfolgreich."
    except Exception as e:
        logger.error(f"!!! FEHLER Nachbearbeitung: {e}\n{traceback.format_exc()}")
        return False, f"Fehler Nachbearbeitung: {e}"
