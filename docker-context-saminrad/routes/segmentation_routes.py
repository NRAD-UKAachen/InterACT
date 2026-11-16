# -*- coding: utf-8 -*-
"""
SAM-Segmentierungs-Routes für RADSAM.
Enthält Routes für die SAM-basierte Segmentierung.
"""

import os
import logging
import traceback
import tempfile
import shutil
import numpy as np
import torch
from PIL import Image
from flask import request, jsonify

from . import segmentation_bp
from config import Config
from services.sam_service import get_predictor, is_sam_available, get_device, collect_sam_annotations_from_request
from services.image_processing import get_slice_from_volume, apply_windowing, save_cutout_image, blend_mask_overlay
from services.archive_service import create_archive_folder, archive_results, save_segmentation_parameters
from services.mask_processing import create_nifti_mask_from_arrays, post_process_nifti_mask, stacking_permuted_affine
from utils.helpers import log_mask_details
import state
import pydicom
import nibabel as nib
from scipy.ndimage import zoom

logger = logging.getLogger(__name__)


@segmentation_bp.route('/segment_image', methods=['POST'])
@segmentation_bp.route('/segment_sam', methods=['POST'])
def segment_image_sam():
    """
    Führt eine SAM-Segmentierung auf einem einzelnen Slice durch.
    Verwendet SAM2VideoPredictor: Erstellt temporären Ordner mit einem Frame,
    initialisiert den VideoPredictor-State und führt die Segmentierung durch.
    """
    if not is_sam_available():
        return jsonify({'success': False, 'error': 'SAM2 ist nicht verfügbar'}), 503

    slices_dir = None
    try:
        data = request.json
        # Akzeptiere sowohl camelCase (JavaScript) als auch snake_case
        slice_index = data.get('slice_index') or data.get('sliceIndex')
        axis = data.get('axis', 'axial')
        points = data.get('points', [])
        labels = data.get('labels', [])
        box_coords = data.get('box')
        ww = data.get('ww')
        wl = data.get('wl')
        base_filename_for_masks = data.get('original_filename', state.CURRENT_ORIGINAL_FILENAME or 'unknown_volume')
        dicom_slice_filename_from_js = data.get('dicom_slice_filename')

        if slice_index is None:
            return jsonify({'success': False, 'error': 'Slice-Index fehlt'}), 400

        # Konvertiere zu int falls nötig
        slice_index = int(slice_index)

        # Hole die Bilddaten
        if state.CURRENT_FILE_TYPE == 'dicom_series':
            if not dicom_slice_filename_from_js:
                if slice_index >= len(state.DICOM_IMAGE_FILES):
                    return jsonify({'success': False, 'error': 'Ungültiger Slice-Index'}), 400
                dicom_path = state.DICOM_IMAGE_FILES[slice_index]
            else:
                dicom_path = next((p for p in state.DICOM_IMAGE_FILES if os.path.basename(p) == dicom_slice_filename_from_js), None)
                if not dicom_path:
                    return jsonify({'success': False, 'error': f'DICOM-Datei {dicom_slice_filename_from_js} nicht gefunden'}), 404

            ds = pydicom.dcmread(dicom_path, force=True)
            pixel_array = ds.pixel_array.astype(np.float32)
            slope = float(getattr(ds, 'RescaleSlope', 1))
            intercept = float(getattr(ds, 'RescaleIntercept', 0))
            if slope != 1 or intercept != 0:
                pixel_array = pixel_array * slope + intercept
            dicom_metadata = ds
            base_name = os.path.splitext(os.path.basename(dicom_path))[0]

        elif state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
            if state.CURRENT_VOLUME_DATA is None:
                return jsonify({'success': False, 'error': 'Kein Volumen geladen'}), 500
            pixel_array = get_slice_from_volume(state.CURRENT_VOLUME_DATA, slice_index, axis)
            if pixel_array is None:
                return jsonify({'success': False, 'error': 'Konnte Slice nicht extrahieren'}), 500
            dicom_metadata = None
            base_name = f"{base_filename_for_masks}_{axis}_{slice_index:03d}"

        else:
            return jsonify({'success': False, 'error': 'Kein gültiger Dateityp geladen'}), 400

        # Fensterung anwenden
        np_rgb, pil_rgb, orig_size, used_ww, used_wl = apply_windowing(pixel_array, ww, wl, dicom_metadata)

        if np_rgb is None:
            return jsonify({'success': False, 'error': 'Konnte Bild für SAM-Eingabe nicht erstellen'}), 500

        # SAM-Segmentierung mit VideoPredictor durchführen
        predictor = get_predictor()

        # Temporären Ordner mit einem Frame erstellen
        slices_dir = tempfile.mkdtemp(dir=state.TEMP_UPLOAD_FOLDER, prefix="single_slice_")
        temp_image = Image.fromarray(np_rgb.astype(np.uint8))
        temp_image.save(os.path.join(slices_dir, "00000.jpg"))

        # VideoPredictor-State initialisieren
        inference_state = predictor.init_state(
            video_path=slices_dir,
            offload_video_to_cpu=False,
            offload_state_to_cpu=False
        )

        # Punkte und Labels vorbereiten
        input_points = None
        input_labels = None
        input_box = None

        logger.info(f"[SAM] slice={slice_index} axis={axis} img_shape=({np_rgb.shape[1]}w x {np_rgb.shape[0]}h) raw_points={points}")

        # JavaScript sendet points als Array von {coords: [x,y], label: 0|1}
        if points:
            point_coords = []
            point_labels = []
            for p in points:
                if isinstance(p, dict):
                    coords = p.get('coords') or p.get('data')
                    if coords:
                        point_coords.append(coords)
                        point_labels.append(p.get('label', 1))
                elif isinstance(p, (list, tuple)) and len(p) >= 2:
                    point_coords.append(p)
                    point_labels.append(labels[len(point_coords)-1] if labels and len(labels) > len(point_coords)-1 else 1)

            if point_coords:
                input_points = torch.tensor(point_coords, dtype=torch.float32)
                input_labels = torch.tensor(point_labels, dtype=torch.int32)

        logger.info(f"[SAM] parsed input_points={input_points} input_labels={input_labels}")

        if box_coords and len(box_coords) == 4:
            input_box = torch.tensor(box_coords, dtype=torch.float32)

        # Prüfen ob Annotationen vorhanden
        if input_box is None and (input_points is None or len(input_points) == 0):
            return jsonify({
                'message': 'Keine Annotationen.',
                'mask_saved': False,
                'overlay_url': None
            }), 200

        # Prompts zum VideoPredictor hinzufügen
        frame_idx, obj_ids, video_res_masks = predictor.add_new_points_or_box(
            inference_state=inference_state,
            frame_idx=0,
            obj_id="segment_1",
            points=input_points,
            labels=input_labels,
            box=input_box,
            clear_old_points=True,
            normalize_coords=True
        )

        # Maske extrahieren
        if len(video_res_masks) == 0:
            return jsonify({
                'message': 'Keine Maske gefunden.',
                'mask_saved': False,
                'overlay_url': None
            }), 200

        masks = video_res_masks[0].cpu().numpy()
        scores = np.array([0.95])  # VideoPredictor gibt keinen expliziten Score

        # Beste Maske binarisieren
        best_mask = masks[0]
        best_mask_binary = (best_mask > 0.5).astype(np.uint8)
        score = float(scores[0])

        logger.info(f"[SAM] mask stats: min={np.min(best_mask):.4f}, max={np.max(best_mask):.4f}, pixels>0.5={np.sum(best_mask_binary)}")

        # Maske auf Originalbildgröße skalieren
        target_size = pil_rgb.size  # (Breite, Höhe)
        mask_pil_unresized = Image.fromarray(best_mask_binary * 255, mode='L')
        mask_pil_resized = mask_pil_unresized.resize(target_size, Image.Resampling.NEAREST)
        resized_mask_np = (np.array(mask_pil_resized) > 128).astype(np.uint8)

        # Archivordner vorbereiten
        archive_folder = create_archive_folder()
        results_folder = os.path.join(archive_folder, "segmentation_results")
        masks_folder = os.path.join(results_folder, "masks")
        overlays_folder = os.path.join(results_folder, "overlays")
        cutouts_folder = os.path.join(results_folder, "cutouts")
        os.makedirs(masks_folder, exist_ok=True)
        os.makedirs(overlays_folder, exist_ok=True)
        os.makedirs(cutouts_folder, exist_ok=True)

        # Skalierte Maske speichern
        mask_filename = f"{base_name}_mask.png"
        mask_path = os.path.join(masks_folder, mask_filename)
        mask_pil_resized.save(mask_path, "PNG")

        # Overlay erstellen und speichern
        overlay_rgb = blend_mask_overlay(np.array(pil_rgb), resized_mask_np == 1, color=(255, 0, 0))

        overlay_filename = f"{base_name}_overlay.png"
        overlay_path = os.path.join(overlays_folder, overlay_filename)
        Image.fromarray(overlay_rgb, 'RGB').save(overlay_path, "PNG")

        # Cutout speichern
        try:
            cutout_filename = f"{base_name}_cutout.png"
            cutout_path = os.path.join(cutouts_folder, cutout_filename)
            save_cutout_image(np.array(pil_rgb), resized_mask_np, cutout_path)
        except Exception as e_cut:
            logger.warning(f"Fehler beim Speichern des Cutouts: {e_cut}")

        mask_area = int(np.sum(best_mask_binary))
        logger.info(f"SAM-Segmentierung erfolgreich für {base_name}, Fläche: {mask_area} Pixel")

        # Overlay-URL für Frontend
        overlay_url = f"/overlay_from_archive/{overlay_filename}"

        # Masken-Daten für Frontend (resized, damit es zu den Rohdaten passt)
        mask_data_for_frontend = resized_mask_np.flatten().astype(np.uint8).tolist()

        return jsonify({
            'message': f'SAM für {base_name} abgeschlossen.',
            'mask_saved': True,
            'overlay_saved': True,
            'overlay_url': overlay_url,
            'score': score,
            'area': mask_area,
            'mask_data': mask_data_for_frontend
        })

    except Exception as e:
        logger.error(f"Fehler bei der SAM-Segmentierung: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500

    finally:
        # Aufräumen: Temporären Slices-Ordner löschen
        if slices_dir:
            try:
                if os.path.exists(slices_dir):
                    shutil.rmtree(slices_dir)
                    logger.debug(f"Temporärer Slices-Ordner gelöscht: {slices_dir}")
            except Exception as e:
                logger.warning(f"Fehler beim Aufräumen des temporären Slices-Ordners: {e}")


@segmentation_bp.route('/create_3d_mask_sam', methods=['POST'])
def create_3d_mask_sam_route():
    """
    Erstellt eine 3D-NIfTI-Maske aus allen gespeicherten SAM-Masken.
    """
    logger.info("--- Route /create_3d_mask_sam gestartet ---")

    try:
        request_data = request.json
        segmentation_axis = request_data.get('axis', 'axial')
        do_fill_holes = request_data.get('fill_holes', False)
        do_remove_outliers = request_data.get('remove_outliers', False)
        min_outlier_size = int(request_data.get('min_outlier_size') or 100)
        current_ww = request_data.get('ww')
        current_wl = request_data.get('wl')
        material_density = request_data.get('material_density')

        if material_density is not None:
            try:
                material_density = float(material_density)
                if material_density <= 0:
                    material_density = None
            except (ValueError, TypeError):
                material_density = None

        logger.debug(f"Parameter: axis={segmentation_axis}, fill_holes={do_fill_holes}, "
                     f"remove_outliers={do_remove_outliers}, min_size={min_outlier_size}, "
                     f"ww={current_ww}, wl={current_wl}, density={material_density}")

        # Archive-Ordner für Masken finden
        archive_folder = create_archive_folder()
        results_folder = os.path.join(archive_folder, "segmentation_results")
        manual_masks_folder = os.path.join(results_folder, "masks")
        fewshot_masks_folder = os.path.join(results_folder, "masks_few_shot")
        s1sfq_masks_folder = os.path.join(results_folder, "masks_s1sfq")
        memory_masks_folder = os.path.join(results_folder, "masks_memory")

        # Automatische Auswahl des Masken-Ordners (Priorität: Memory > S1SFQ > Interpolation > manuell)
        active_folder, fewshot_mode = _get_active_fewshot_folder(
            memory_masks_folder, s1sfq_masks_folder, fewshot_masks_folder, manual_masks_folder
        )

        # Masken laden
        mask_arrays, ref_shape_2d, affine_for_nifti, header_for_nifti, files_for_affine_calc = \
            _load_masks_for_3d(active_folder, manual_masks_folder, segmentation_axis)

        if not mask_arrays or ref_shape_2d is None:
            return jsonify({'success': False, 'error': 'Keine Masken oder Referenz-Shape gefunden'}), 500

        # Archive-Ordner für 3D-Modelle vorbereiten
        archive_nifti_folder = os.path.join(results_folder, "3d_models")
        os.makedirs(archive_nifti_folder, exist_ok=True)

        # Analyse-Maske erstellen
        logger.info("Erstelle 'Analyse-Maske' NIfTI...")
        success_analysis, analysis_nifti_filename, message, volume_cm3, weight_g = create_nifti_mask_from_arrays(
            files_for_affine_calc, mask_arrays, archive_nifti_folder,
            nifti_file_suffix=f"_{state.CURRENT_ORIGINAL_FILENAME}_{segmentation_axis}_SAM_analysis_mask.nii.gz",
            input_affine=affine_for_nifti,
            input_header=header_for_nifti,
            downsample_factor=state.ANALYSIS_MASK_DOWNSAMPLE_FACTOR,
            material_density=material_density
        )

        if not success_analysis:
            logger.error(f"Abbruch: Erstellung der Analyse-Maske fehlgeschlagen. Nachricht: {message}")
            return jsonify({'success': False, 'error': f'Fehler Erstellung Analyse-Maske: {message}'}), 500

        analysis_nifti_path = os.path.join(archive_nifti_folder, analysis_nifti_filename)

        # Nachbearbeitung
        if do_fill_holes or do_remove_outliers:
            logger.info("Starte Nachbearbeitung der Analyse-Maske...")
            post_success, post_message = post_process_nifti_mask(
                analysis_nifti_path, analysis_nifti_path,
                fill_holes=do_fill_holes, remove_outliers=do_remove_outliers, min_outlier_size=min_outlier_size
            )
            message += f" | Nachbearb.: {post_message}"

        # Viewer-Maske erstellen (gedownsampled)
        viewer_nifti_filename = f"viewer_{analysis_nifti_filename}"
        viewer_nifti_path = os.path.join(archive_nifti_folder, viewer_nifti_filename)
        nifti_filename_to_serve = _create_viewer_mask(
            analysis_nifti_path, viewer_nifti_path, analysis_nifti_filename
        )

        # URL erstellen
        nifti_url = f"/nifti_masks_sam/{nifti_filename_to_serve}" if nifti_filename_to_serve else None

        # Annotationen sammeln
        sam_annotations = collect_sam_annotations_from_request(request_data)

        # Segmentierungsparameter
        segmentation_params = {
            "method": "sam",
            "parameters": {
                "segmentation_axis": segmentation_axis,
                "fill_holes": do_fill_holes,
                "remove_outliers": do_remove_outliers,
                "min_outlier_size": min_outlier_size,
                "window_width": current_ww,
                "window_level": current_wl,
                "material_density": material_density
            },
            "processing": {
                "analysis_downsample_factor": state.ANALYSIS_MASK_DOWNSAMPLE_FACTOR,
                "viewer_downsample_factor": state.VIEWER_SPECIFIC_DOWNSAMPLE_FACTOR
            },
            "output_files": {
                "analysis_nifti": analysis_nifti_filename,
                "viewer_nifti": viewer_nifti_filename
            },
            "annotations": sam_annotations,
            "volume_analysis": {
                "volume_cm3": volume_cm3,
                "weight_g": weight_g,
                "material_density_g_cm3": material_density
            }
        }

        # Archivieren
        archive_success = archive_results(segmentation_params=segmentation_params)
        if archive_success:
            message += " | Ergebnisse wurden archiviert."

        # Finale Overlay-Dateien sammeln für das Frontend
        final_overlay_files = []
        if os.path.isdir(active_folder):
            mask_files = sorted([f for f in os.listdir(active_folder) if f.endswith('_mask.png')])
            logger.info(f"Found {len(mask_files)} mask files in {active_folder}")

            # Bestimme den Overlay-Ordner basierend auf der aktiven Methode
            if fewshot_mode == 'memory':
                overlay_folder = os.path.join(results_folder, "overlays_memory")
            elif fewshot_mode == 's1sfq':
                overlay_folder = os.path.join(results_folder, "overlays_s1sfq")
            elif fewshot_mode == 'few_shot':
                overlay_folder = os.path.join(results_folder, "overlays_few_shot")
            else:
                overlay_folder = os.path.join(results_folder, "overlays")

            logger.info(f"Lade Overlays aus: {overlay_folder}")

            for mask_file in mask_files:
                overlay_file = mask_file.replace('_mask.png', '_overlay.png')
                overlay_path = os.path.join(overlay_folder, overlay_file)
                if os.path.exists(overlay_path):
                    final_overlay_files.append(overlay_file)
                else:
                    logger.warning(f"Overlay file not found: {overlay_path}")

        logger.info(f"Sending {len(final_overlay_files)} overlay files to frontend")

        # Antwort vorbereiten
        response_data = {
            "success": True,
            "message": message,
            "nifti_filename": nifti_filename_to_serve,
            "nifti_url": nifti_url,
            "method": "sam",
            "active_overlay_source": fewshot_mode,
            "final_overlay_files": final_overlay_files
        }

        if volume_cm3 is not None:
            response_data["volume_cm3"] = volume_cm3
        if weight_g is not None:
            response_data["weight_g"] = weight_g
        if material_density is not None:
            response_data["material_density"] = material_density

        logger.info(f"3D SAM Maske erfolgreich erstellt: {nifti_filename_to_serve}")
        return jsonify(response_data)

    except Exception as e:
        logger.error(f"Fehler bei der 3D-Masken-Erstellung: {e}")
        logger.debug(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500


def _get_active_fewshot_folder(memory_folder, s1sfq_folder, fewshot_folder, manual_folder):
    """Bestimmt den aktiven Masken-Ordner basierend auf Priorität: Memory > S1SFQ > Interpolation > manuell."""
    if os.path.isdir(memory_folder):
        memory_files = [f for f in os.listdir(memory_folder) if f.endswith('_mask.png')]
        if len(memory_files) > 0:
            logger.info(f"Nutze Memory-Attention-Masken für 3D-Modell: {memory_folder}")
            return memory_folder, 'memory'

    if os.path.isdir(s1sfq_folder):
        s1sfq_files = [f for f in os.listdir(s1sfq_folder) if f.endswith('_mask.png')]
        if len(s1sfq_files) > 0:
            logger.info(f"Nutze S1SFQ-Masken für 3D-Modell: {s1sfq_folder}")
            return s1sfq_folder, 's1sfq'

    if os.path.isdir(fewshot_folder):
        fewshot_files = [f for f in os.listdir(fewshot_folder) if f.endswith('_mask.png')]
        if len(fewshot_files) > 0:
            logger.info(f"Nutze Interpolations-Few-Shot-Masken für 3D-Modell: {fewshot_folder}")
            return fewshot_folder, 'few_shot'

    logger.info(f"Nutze manuelle Masken für 3D-Modell: {manual_folder}")
    return manual_folder, 'manual'


def _load_masks_for_3d(active_folder, manual_folder, segmentation_axis):
    """Lädt alle Masken für die 3D-Erstellung."""
    mask_arrays = []
    ref_shape_2d = None
    affine_for_nifti = None
    header_for_nifti = None
    files_for_affine_calc = []

    def load_prioritized_mask(mask_basename):
        """Lädt Maske aus aktivem oder manuellem Ordner."""
        fewshot_path = os.path.join(active_folder, mask_basename)
        if os.path.exists(fewshot_path):
            return Image.open(fewshot_path).convert('L')
        manual_path = os.path.join(manual_folder, mask_basename)
        if os.path.exists(manual_path):
            return Image.open(manual_path).convert('L')
        return None

    if state.CURRENT_FILE_TYPE == 'dicom_series':
        files_for_affine_calc = [os.path.basename(p) for p in state.DICOM_IMAGE_FILES]
        for dicom_filepath in state.DICOM_IMAGE_FILES:
            base = os.path.splitext(os.path.basename(dicom_filepath))[0]
            mask_basename = f"{base}_mask.png"
            mask_pil = load_prioritized_mask(mask_basename)

            try:
                if mask_pil:
                    mask_np = (np.array(mask_pil) > 128).astype(np.uint8)
                    if ref_shape_2d is None:
                        ref_shape_2d = mask_np.shape
                    mask_arrays.append(mask_np if mask_np.shape == ref_shape_2d else np.zeros(ref_shape_2d, dtype=np.uint8))
                else:
                    if ref_shape_2d is None:
                        temp_ds = pydicom.dcmread(dicom_filepath)
                        ref_shape_2d = temp_ds.pixel_array.shape
                    mask_arrays.append(np.zeros(ref_shape_2d, dtype=np.uint8))
            except Exception as e:
                logger.warning(f"Fehler beim Verarbeiten der Maske für {base}: {e}")
                if ref_shape_2d:
                    mask_arrays.append(np.zeros(ref_shape_2d, dtype=np.uint8))

    elif state.CURRENT_FILE_TYPE in ['nrrd', 'nifti']:
        num_slices = state.SLICE_COUNTS.get(segmentation_axis, 0)
        files_for_affine_calc = [state.CURRENT_ORIGINAL_FILENAME]

        for i in range(num_slices):
            mask_basename = f"{state.CURRENT_ORIGINAL_FILENAME}_{segmentation_axis}_{i:03d}_mask.png"
            mask_pil = load_prioritized_mask(mask_basename)

            try:
                if mask_pil:
                    mask_np = (np.array(mask_pil) > 128).astype(np.uint8)
                    if ref_shape_2d is None:
                        ref_shape_2d = mask_np.shape
                    mask_arrays.append(mask_np if mask_np.shape == ref_shape_2d else np.zeros(ref_shape_2d, dtype=np.uint8))
                else:
                    if ref_shape_2d is None:
                        temp_slice = get_slice_from_volume(state.CURRENT_VOLUME_DATA, i, segmentation_axis)
                        if temp_slice is not None:
                            ref_shape_2d = temp_slice.shape
                    if ref_shape_2d:
                        mask_arrays.append(np.zeros(ref_shape_2d, dtype=np.uint8))
            except Exception as e:
                logger.warning(f"Fehler beim Verarbeiten der Maske für Slice {i}: {e}")
                if ref_shape_2d:
                    mask_arrays.append(np.zeros(ref_shape_2d, dtype=np.uint8))

        # Affine passend zur Stapel-Achsenreihenfolge permutieren (siehe _stacking_permuted_affine):
        # sonst wird bei anisotropem Spacing (z.B. 8 mm) das 3D-Modell entlang der falschen Achse gestreckt.
        affine_for_nifti = stacking_permuted_affine(state.CURRENT_VOLUME_AFFINE, segmentation_axis)
        _perm_zooms = np.sqrt(np.sum(affine_for_nifti[:3, :3] ** 2, axis=0))
        _perm_zooms[~np.isfinite(_perm_zooms) | (_perm_zooms <= 1e-6)] = 1.0
        header_for_nifti = nib.Nifti1Header()
        header_for_nifti.set_data_dtype(np.uint8)
        header_for_nifti.set_data_shape(state.CURRENT_VOLUME_DATA.shape)  # ndim=3, sonst crasht set_zooms
        header_for_nifti.set_zooms(tuple(float(z) for z in _perm_zooms[:3]))

    return mask_arrays, ref_shape_2d, affine_for_nifti, header_for_nifti, files_for_affine_calc


def _create_viewer_mask(analysis_nifti_path, viewer_nifti_path, analysis_nifti_filename):
    """Erstellt eine gedownsampelte Viewer-Maske."""
    try:
        logger.info(f"[Viewer Mask SAM] Erzeuge Viewer-Version von: {analysis_nifti_path}")
        img_for_viewer = nib.load(analysis_nifti_path)
        data_for_viewer = img_for_viewer.get_fdata().astype(np.uint8)
        affine_for_viewer = img_for_viewer.affine.copy()
        header_for_viewer = img_for_viewer.header.copy()

        downsampled_viewer_data = zoom(
            data_for_viewer, state.VIEWER_SPECIFIC_DOWNSAMPLE_FACTOR, order=0, mode='nearest'
        ).astype(np.uint8)

        log_mask_details(downsampled_viewer_data, f"VIEWER Maskendaten SAM (Shape: {downsampled_viewer_data.shape})")

        new_affine_viewer = affine_for_viewer.copy()
        original_zooms_viewer = np.sqrt(np.sum(new_affine_viewer[:3, :3] ** 2, axis=0))
        original_zooms_viewer[~np.isfinite(original_zooms_viewer) | (original_zooms_viewer <= 1e-6)] = 1.0

        for i in range(3):
            new_affine_viewer[:3, i] = new_affine_viewer[:3, i] / state.VIEWER_SPECIFIC_DOWNSAMPLE_FACTOR

        header_for_viewer.set_data_dtype(np.uint8)
        header_for_viewer.set_data_shape(downsampled_viewer_data.shape)
        new_zooms_for_viewer_header = original_zooms_viewer / state.VIEWER_SPECIFIC_DOWNSAMPLE_FACTOR
        header_for_viewer.set_zooms(new_zooms_for_viewer_header[:3])

        nifti_img_viewer = nib.Nifti1Image(downsampled_viewer_data, new_affine_viewer, header=header_for_viewer)
        nifti_img_viewer.set_sform(new_affine_viewer, code=1)
        nifti_img_viewer.set_qform(new_affine_viewer, code=1)
        nib.save(nifti_img_viewer, viewer_nifti_path)

        logger.info(f"✅ Viewer NIfTI Maske SAM gespeichert: {viewer_nifti_path}")
        return os.path.basename(viewer_nifti_path)

    except Exception as e:
        logger.error(f"Fehler beim Erstellen der Viewer-Maske: {e}")
        return analysis_nifti_filename
