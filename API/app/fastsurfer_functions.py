import os
import shutil
import numpy as np
import nibabel as nib
import pandas as pd
from scipy.ndimage import affine_transform

from .job_logging import run_logged
from .backend_helpers import (
    choose_gpu_index, client, free_gpu_memory_gb, log, rasterize_figure,
    store_required_series_as_nifti, upload_dataframe_as_sc, upload_images_as_sc,
    upload_segmentation,
)
from .storage_paths import API_STORAGE_DIR

FASTSURFER_STORAGE_ROOT = f'{API_STORAGE_DIR}/fastsurfer'
# Pinned rather than :latest so a pull can't silently change what the numbers
# in PACS were produced by. Shared with the longitudinal pipeline
# (fastsurfer_long_functions.py), which drives the very same image's
# long_fastsurfer.sh -- the two must stay on one version, since a longitudinal
# run's timepoints are only comparable to each other and to a cross-sectional
# run if all of them came out of the same networks.
FASTSURFER_IMAGE = 'deepmi/fastsurfer:cu118-v2.5.4'
# HOST dir containing your FreeSurfer license.txt, for the deepmi/fastsurfer
# image. Mounted read-only and kept separate from the per-study I/O dirs
# (mounted below), since neither input nor output dir is the whole checkout.
#
# This has to be a *host* path, not a path in this container: FastSurfer runs as
# a sibling container on the host Docker daemon, so the -v source below is
# resolved by the daemon, not by us. FS_LICENSE_DIR is set in the root .env and
# passed through by API/docker-compose.yml, which also bind-mounts the same
# directory here at /fs_license so ensure_fs_license() can check it.
FASTSURFER_LICENSE_DIR = os.environ.get('FS_LICENSE_DIR', '').rstrip('/')
if not FASTSURFER_LICENSE_DIR:
    raise RuntimeError('FS_LICENSE_DIR must be set in the root .env')
# Where that same directory is mounted inside THIS container, so we can tell a
# missing licence apart from a broken run before launching anything.
FS_LICENSE_LOCAL = '/fs_license/license.txt'

FS_LICENSE_HELP = (
    "FreeSurfer license.txt not found. FastSurfer, SynthSeg and the "
    "hippocampus pipeline all need one; it is free but must be requested "
    "individually at https://surfer.nmr.mgh.harvard.edu/registration.html. "
    "Save the file it mails you as secrets/freesurfer/license.txt, or point "
    "FS_LICENSE_DIR in the root .env at the directory holding it. "
    "See secrets/freesurfer/README.md."
)


def ensure_fs_license():
    """Raise a self-explanatory error if the FreeSurfer licence is missing.

    Without this the failure surfaces from inside the sibling container as
    FreeSurfer's own "license file /opt/freesurfer/.license not found" and an
    exit code, several layers away from the thing the user actually has to fix.
    """
    if not os.path.isfile(FS_LICENSE_LOCAL):
        raise RuntimeError(FS_LICENSE_HELP)

ASEG_STATS_COLUMNS = ['Index', 'SegId', 'NVoxels', 'Volume_mm3', 'StructName',
                      'normMean', 'normStdDev', 'normMin', 'normMax', 'normRange']
APARC_STATS_COLUMNS = ['StructName', 'NumVert', 'SurfArea', 'GrayVol', 'ThickAvg',
                        'ThickStd', 'MeanCurv', 'GausCurv', 'FoldInd', 'CurvInd']

CORTICAL_THICKNESS_VIEWS = ('lateral', 'medial', 'anterior', 'posterior')
_HEMI_NAMES = {'lh': 'left', 'rh': 'right'}

# Rows whose |left-right asymmetry| exceeds this (in percent of the pair's
# mean, see _asymmetry_percent) are colour-highlighted in the rendered
# volume/thickness tables, so a marked side difference is visible at a glance
# instead of having to be read out of the numbers. 10% is a reporting
# convention, not a diagnostic cut-off -- the number stays in the table either
# way, and the highlight only draws the eye to it.
ASYMMETRY_HIGHLIGHT_PERCENT = 10

# Global measures pulled out of stats/brainvol.stats (recon-surf's whole-brain
# summary -- see _parse_brainvol_stats) for the "Global Brain Volumes" table:
# (table label, brainvol.stats key). Chosen to match what's actually asked
# for in practice -- total brain, total grey and total white matter -- over
# the dozen-odd other measures that file carries (BrainSegNotVent, eTIV-style
# mask volume, per-hemisphere splits, ...), which stay unused rather than
# cluttering a three-row summary.
#
# 'BrainSeg' (Brain Segmentation Volume) is FreeSurfer/FastSurfer's standard
# whole-brain volume measure and what papers mean by "total brain volume".
# 'TotalGray' already sums cortical *and* subcortical grey matter, so it is
# used as-is rather than added up from the per-region table above -- doing
# that by hand would mean maintaining a second copy of FreeSurfer's own
# grey-matter structure list. 'CerebralWhiteMatter' is the analogous total
# for white matter (cerebellar white matter and brainstem are excluded, same
# as FreeSurfer's own definition).
GLOBAL_VOLUME_MEASURES = (
    ('Total brain volume', 'BrainSeg'),
    ('Total grey matter volume', 'TotalGray'),
    ('Total white matter volume', 'CerebralWhiteMatter'),
)

# --- GPU memory preflight for the segmentation stage ------------------------
# FastSurfer conforms to the smallest voxel side ("--vox_size min", the
# run_fastsurfer.sh default), except that anything coarser than this threshold
# snaps to 1 mm; mirrors conform_to_1mm_threshold in FastSurferCNN's
# run_prediction.py, whose default this matches.
CONFORM_TO_1MM_THRESHOLD = 0.95
CONFORM_MIN_SIDE = 256
# View aggregation on the GPU costs "approx. 5 GB for standard 256x256x256
# images" (FastSurfer's own figure, quoted in its out-of-memory message) and
# scales with the voxel count, so a 0.49 mm acquisition conformed to 512^3
# wants roughly eight times that. SEGMENTATION_GPU_GB is what the networks
# themselves occupy alongside it, measured off the per-process figures in
# FastSurfer's CUDA OOM reports (~1.2-1.5 GB), rounded up.
VIEWAGG_GPU_GB_AT_256 = 5.0
SEGMENTATION_GPU_GB = 2.0


### ---------------------------------------------------------------------- ###
### pure local-file compute -- no dcm4chee access below this point         ###
### ---------------------------------------------------------------------- ###

def _clear_previous_subject_dir(output_dir, sid):
    """Delete output_dir/<sid> if an earlier run left one behind, so every run
    starts from a clean subject directory.

    recon-surf.sh (the surface pipeline, mode='full') hard-refuses to run when
    the subject dir already holds output from a previous invocation --
    "ERROR: Running on top of an existing subject directory!", tripped by
    mri/wm.mgz or mri/aparc.DKTatlas+aseg.orig.mgz existing. The second of
    those is written by the *segmentation* stage, so without this the
    interactive route fails on any repeat click for a study: 'full' after
    'full', and also 'full' after a plain 'seg' run, since output_dir is
    per-study (see fastsurfer_wrapper) and gets reused.

    Deleting is safe because this tree is pure scratch: nothing serves files
    out of FASTSURFER_STORAGE_ROOT (unlike hippocampus_functions.py's
    DOWNLOADABLE_FILES), a previous run's products are already in PACS or in
    the batch pipeline's own out_dir, and the staged t1n.nii.gz input lives in
    the parent study dir, not in here. (Resuming instead -- feeding an
    existing segmentation to run_fastsurfer.sh's --surf_only to skip
    re-segmenting -- would be a genuine speedup, but only when the previous
    run's seg output is known complete and produced with the same options,
    which isn't tracked anywhere yet.)"""
    subject_dir = os.path.join(output_dir, sid)
    if os.path.exists(subject_dir):
        log(f"Removing previous FastSurfer output at {subject_dir}")
        shutil.rmtree(subject_dir)


def _require_surface_outputs(subject_dir, stats_dir, surf_dir, include_renders):
    """Fail loudly if the surface pipeline didn't produce what mode='full'
    reads, instead of quietly reporting something else as this run's result.

    This is not belt-and-braces: run_fastsurfer.sh exits 0 even when
    recon-surf.sh refused to run at all. Observed on 2026-08-17 -- a repeat
    'full' click hit the existing-subject-directory refusal
    (_clear_previous_subject_dir), run_fastsurfer.sh still returned 0, so
    _run_fastsurfer_container raised nothing and the wrapper went on to read
    lh/rh.aparc.DKTatlas.mapped.stats and surf/ from the *previous* run and
    upload tables and renders built from them, logging a clean "Finished
    full FastSurfer Pipeline". A stale reconstruction of the same study is
    plausible enough to go unnoticed, which is exactly what makes it worth an
    explicit check: the exit code alone cannot tell us the surfaces in here
    belong to this run.

    Checking mere presence is sufficient *because* _clear_previous_subject_dir
    empties the subject dir first, so anything found here was written by this
    run -- the two go together."""
    required = [os.path.join(stats_dir, f'{hemi}.aparc.DKTatlas.mapped.stats')
                for hemi in ('lh', 'rh')]
    if include_renders:
        required += [os.path.join(surf_dir, f'{hemi}.{f}')
                     for hemi in ('lh', 'rh') for f in ('pial', 'thickness', 'curv')]

    missing = [p for p in required if not os.path.exists(p)]
    if missing:
        raise RuntimeError(
            "FastSurfer's surface pipeline (recon-surf) did not produce its "
            "expected output, so there is no cortical thickness result for this "
            "run -- note that run_fastsurfer.sh can still exit 0 in this case.\n"
            "Missing:\n  " + "\n  ".join(missing) + "\n"
            f"See {os.path.join(subject_dir, 'scripts', 'recon-surf.log')} for why."
        )


def _conformed_grid_side(t1_path):
    """The cube side FastSurfer will conform this T1 to, replicating
    conform.py's sizing for "--vox_size min": conform to the smallest voxel
    side, snap anything coarser than CONFORM_TO_1MM_THRESHOLD to 1 mm, and
    size the grid off the field of view, cubed on its largest side.

    A 1x1x1 mm acquisition lands on 256^3; the 0.49x0.49x1.0 mm ones this
    department exports at a 512 matrix land on 512^3 -- eight times the
    voxels, which is what makes view aggregation outgrow a shared GPU.
    """
    img = nib.load(t1_path)
    zooms = np.asarray(img.header.get_zooms()[:3], dtype=float)
    vox_size = min(round(float(zooms.min()), 4), 1.0)
    if vox_size > CONFORM_TO_1MM_THRESHOLD:
        vox_size = 1.0
    # conform.py's own tolerance for "this is a 1 mm image, use 256^3".
    if abs(vox_size - 1.0) <= 1.0 - CONFORM_TO_1MM_THRESHOLD:
        return CONFORM_MIN_SIDE

    fov = zooms * np.asarray(img.shape[:3], dtype=float)
    return max(CONFORM_MIN_SIDE, int(np.ceil(fov / vox_size).max()))


def _view_aggregation_device(t1_paths, gpu_index):
    """Return 'cpu' when GPU view aggregation would not fit in what is
    currently free on the GPU this run will use, else None -- meaning leave
    FastSurfer's own '--viewagg_device auto' alone.

    `t1_paths` is every T1 the run will segment -- one for a cross-sectional
    run, one per timepoint for a longitudinal one -- and the largest conform
    among them decides, since a single flag covers the whole run.

    Worth checking ourselves because FastSurfer's 'auto' compares a flat 4 GB
    minimum against the card's *total* memory (find_device() in
    FastSurferCNN/utils/common.py), so on an 80 GB card it always picks the
    GPU -- no matter how much of it another process already holds, and no
    matter how large the conformed image is. When that guess is wrong the
    segmentation aborts with 'INSUFFICIENT GPU MEMORY' but run_fastsurfer.sh
    carries on for another hour building surfaces from an aseg that was never
    written, so the cost of finding out late is high.

    Only view aggregation moves to the CPU; the networks still run on the GPU,
    which costs a few minutes rather than the hours a full CPU run would.
    """
    side = max(_conformed_grid_side(path) for path in t1_paths)
    required_gb = SEGMENTATION_GPU_GB + VIEWAGG_GPU_GB_AT_256 * (side / CONFORM_MIN_SIDE) ** 3

    # gpu_index None (only when choose_gpu_index could not read nvidia-smi)
    # means the container gets '--gpus all', and FastSurfer's '--device auto'
    # then resolves to cuda:0 -- so GPU 0 is the one whose headroom decides.
    device_index = 0 if gpu_index is None else gpu_index
    free_gb = free_gpu_memory_gb(device_index)
    if free_gb is None:
        log("Could not read free GPU memory, leaving view aggregation on auto")
        return None

    if free_gb >= required_gb:
        return None

    log(f"GPU {device_index} has {free_gb:.1f} GB free but a {side}^3 conform needs "
        f"about {required_gb:.1f} GB, running view aggregation on the CPU")
    return 'cpu'


def _run_fastsurfer_container(input_dir, output_dir, sid, mode='full', gpu_index=None):
    """Run the FastSurfer container against input_dir/t1n.nii.gz, writing the
    subject's output to output_dir/<sid>. mode='seg' does whole-brain
    segmentation only; mode='full' also runs the surface pipeline (cortical
    thickness/parcellation).

    gpu_index pins the container to one GPU (`--gpus device=<idx>`) instead
    of handing it every GPU on the box -- required by batch_jobs.py's worker
    pool, where several of these run at once and must not all default
    to/contend for the same device. The interactive per-study route
    gets the emptiest card from fastsurfer_core (choose_gpu_index); None,
    when that can't be read, falls back to `--gpus all`."""
    ensure_fs_license()
    gpus_arg = 'all' if gpu_index is None else f'device={gpu_index}'
    cmd = [
        'docker', 'run', '--rm', '--gpus', gpus_arg,
        '--user', f'{os.getuid()}:{os.getgid()}',
        '-v', f'{input_dir}:/input:ro',
        '-v', f'{output_dir}:/output',
        '-v', f'{FASTSURFER_LICENSE_DIR}:/fs_license:ro',
        FASTSURFER_IMAGE,
        '--fs_license', '/fs_license/license.txt',
        '--t1', '/input/t1n.nii.gz',
        '--sid', sid, '--sd', '/output',
        '--3T',
        '--threads', '4',
    ]
    viewagg_device = _view_aggregation_device([os.path.join(input_dir, 't1n.nii.gz')], gpu_index)
    if viewagg_device is not None:
        # One flag covers all three GPU stages -- run_fastsurfer.sh forwards it
        # to the aseg+DKT, CerebNet and HypVINN predictions alike.
        cmd += ['--viewagg_device', viewagg_device]
    if mode == 'seg':
        # Whole-brain segmentation only; skips the surface pipeline, so no
        # cortical thickness/parcellation stats are produced in this mode.
        cmd.append('--seg_only')
    if os.getuid() == 0:
        # The ohif_api container itself runs as root (no USER in its
        # Dockerfile), so --user above is 0:0 -- run_fastsurfer.sh refuses to
        # proceed as root unless explicitly told to.
        cmd.append('--allow_root')

    _clear_previous_subject_dir(output_dir, sid)

    log(f"Running FastSurfer ({mode}) for subject {sid}")
    result = run_logged(cmd, name='fastsurfer')
    if result.returncode != 0:
        raise RuntimeError(
            f"FastSurfer error (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )


def _read_stats(path, columns):
    """Read a whitespace-separated FreeSurfer/FastSurfer .stats file (comment
    lines starting with '#' are skipped) into a DataFrame with `columns`."""
    return pd.read_csv(path, comment='#', sep=r'\s+', names=columns)


def _segmentation_labels_from_aseg_stats(aseg_stats_path):
    """Build the (labels, values) upload_segmentation() needs directly from
    the aseg+DKT stats file's SegId/StructName columns, instead of
    maintaining a separate copy of FreeSurfer's ~1000-entry color LUT for the
    ~95 labels FastSurfer actually produces."""
    aseg_df = _read_stats(aseg_stats_path, ASEG_STATS_COLUMNS)
    return list(aseg_df['StructName']), list(aseg_df['SegId'])


def _select_segmentation_and_stats(mri_dir, stats_dir):
    """Pick the whole-brain segmentation volume and its matching aseg+DKT
    stats file together, so labels/values always describe the exact volume
    being uploaded. Prefers the post-corpus-callosum-labeling pair
    (*.withCC*), falling back to the pre-CC pair if that wasn't generated."""
    candidates = [
        ('aparc.DKTatlas+aseg.deep.withCC.mgz', 'aseg+DKT.VINN.withCC.stats'),
        ('aparc.DKTatlas+aseg.deep.mgz', 'aseg+DKT.VINN.stats'),
    ]
    for seg_name, stats_name in candidates:
        seg_path = os.path.join(mri_dir, seg_name)
        stats_path = os.path.join(stats_dir, stats_name)
        if os.path.exists(seg_path) and os.path.exists(stats_path):
            return seg_path, stats_path

    raise FileNotFoundError(
        f"Could not find a matching FastSurfer segmentation/stats pair in {mri_dir} / {stats_dir}"
    )


def _resample_segmentation_to_t1_grid(seg_path, t1_nifti_path):
    """FastSurfer conforms its output to a 256^3, 1mm-isotropic grid, which
    doesn't match t1n.nii.gz's original shape/spacing -- upload_segmentation()
    needs the two to line up voxel-for-voxel. Resample the segmentation back
    onto t1n.nii.gz's grid (nearest-neighbor, to preserve label values)."""
    seg_img = nib.load(seg_path)
    seg_data = seg_img.get_fdata()
    seg_affine = seg_img.affine

    ref_img = nib.load(t1_nifti_path)

    voxel_map = np.linalg.inv(seg_affine) @ ref_img.affine
    return affine_transform(
        seg_data, voxel_map[:3, :3], offset=voxel_map[:3, 3],
        output_shape=ref_img.shape, order=0, mode='constant', cval=0,
    )


def _asymmetry_percent(left, right):
    """Left-right asymmetry as a percentage of the pair's mean -- the standard
    (L-R)/mean(L,R) index, positive meaning left > right. Shared by the volume
    and the cortical thickness table so FastSurfer's two reports quote the
    same quantity (and matches the sign convention of hippocampus_functions.py's
    _asymmetry_index, which reports the same thing unscaled).

    Works elementwise on DataFrame columns. A pair that is 0 on both sides (a
    region absent from this subject's parcellation) yields NaN rather than a
    divide-by-zero.
    """
    mean = (left + right) / 2
    return np.where(mean != 0, (left - right) / mean * 100, np.nan)


def _asymmetry_highlight(table, column, threshold=ASYMMETRY_HIGHLIGHT_PERCENT):
    """Per-row booleans marking |asymmetry| above the threshold, for
    upload_dataframe_as_sc()'s highlight_rows. Direction-agnostic (left>right
    and right>left both flag), and a NaN asymmetry (region absent on both
    sides) compares False, so a missing region never lights up."""
    return (table[column].abs() > threshold).tolist()


def _highlight_note(threshold=ASYMMETRY_HIGHLIGHT_PERCENT):
    """Caption for the highlighted tables, so the colour is explained in the
    rendered image itself -- it is uploaded to PACS on its own, away from any
    other context."""
    return f'Highlighted rows: left-right asymmetry above {threshold}%'


def _hemisphere_volume_table(aseg_stats_path):
    """Left/right volume (and other aseg.stats columns) comparison, for both
    subcortical structures (Left-/Right- prefix) and DKT cortical regions
    (ctx-lh-/ctx-rh- prefix)."""
    aseg_df = _read_stats(aseg_stats_path, ASEG_STATS_COLUMNS)

    left = aseg_df[aseg_df['StructName'].str.startswith(('Left-', 'ctx-lh-'))].copy()
    right = aseg_df[aseg_df['StructName'].str.startswith(('Right-', 'ctx-rh-'))].copy()
    left['Region'] = left['StructName'].str.replace('Left-', '', regex=False).str.replace('ctx-lh-', '', regex=False)
    right['Region'] = right['StructName'].str.replace('Right-', '', regex=False).str.replace('ctx-rh-', '', regex=False)

    table = left[['Region', 'Volume_mm3']].merge(
        right[['Region', 'Volume_mm3']], on='Region', suffixes=('_Left', '_Right')
    )
    table['Asymmetry_%'] = _asymmetry_percent(
        table['Volume_mm3_Left'], table['Volume_mm3_Right']
    )
    return table.round(2)


def _cortical_thickness_table(stats_dir):
    """Cortical thickness/area/volume per DKT region, left vs right hemisphere.
    Only available in mode='full' (recon-surf produces
    lh/rh.aparc.DKTatlas.mapped.stats -- FastSurfer's DKT-atlas parcellation,
    not classic FreeSurfer's lh/rh.aparc.stats).

    Thickness gets a left-right asymmetry column, next to the two values it
    is computed from; area and volume deliberately don't. Mean thickness is
    the same kind of quantity on both sides (a per-region average in mm), so
    the L-R index is directly interpretable, whereas surface area and gray
    matter volume scale with each hemisphere's overall size and their raw
    difference mostly reflects head/hemisphere size rather than a regional
    finding."""
    lh_df = _read_stats(os.path.join(stats_dir, 'lh.aparc.DKTatlas.mapped.stats'), APARC_STATS_COLUMNS)
    rh_df = _read_stats(os.path.join(stats_dir, 'rh.aparc.DKTatlas.mapped.stats'), APARC_STATS_COLUMNS)

    table = lh_df[['StructName', 'ThickAvg', 'SurfArea', 'GrayVol']].merge(
        rh_df[['StructName', 'ThickAvg', 'SurfArea', 'GrayVol']],
        on='StructName', suffixes=('_Left', '_Right')
    ).rename(columns={'StructName': 'Region'})

    table['ThickAvg_Asymmetry_%'] = _asymmetry_percent(
        table['ThickAvg_Left'], table['ThickAvg_Right']
    )

    # The merge above interleaves the columns as lh's three then rh's three;
    # group them per metric instead, so each Left/Right pair (and the
    # asymmetry derived from the thickness pair) reads together in the
    # rendered table.
    return table[[
        'Region',
        'ThickAvg_Left', 'ThickAvg_Right', 'ThickAvg_Asymmetry_%',
        'SurfArea_Left', 'SurfArea_Right',
        'GrayVol_Left', 'GrayVol_Right',
    ]].round(2)


def _parse_brainvol_stats(brainvol_stats_path):
    """Parse recon-surf's stats/brainvol.stats into a {key: value_mm3} dict,
    keyed by the measure's short name (e.g. 'BrainSeg', 'TotalGray').

    The file is mri_brainvol_stats.py's output, not a fixed-width table like
    aseg.stats -- each line of interest reads
    '# Measure <key>, <header>, <description>, <value>, <unit>' (see a real
    one for the exact numbers: stats/brainvol.stats in any mode='full'
    output). Split on ',' and taken positionally rather than matched by
    column name, the same way parse_siena_report/parse_sienax_report in
    siena_functions.py read FSL's plain-text reports."""
    measures = {}
    with open(brainvol_stats_path) as f:
        for line in f:
            if not line.startswith('# Measure'):
                continue
            parts = [p.strip() for p in line[len('# Measure'):].split(',')]
            if len(parts) < 4:
                continue
            key, value = parts[0], parts[3]
            try:
                measures[key] = float(value)
            except ValueError:
                continue
    return measures


def _global_volume_table(brainvol_stats_path):
    """Total brain / grey matter / white matter volume, for the "Global Brain
    Volumes" table -- see GLOBAL_VOLUME_MEASURES for why these three and not
    the rest of what brainvol.stats carries.

    Only available in mode='full': brainvol.stats is written by recon-surf's
    mri_brainvol_stats.py, so mode='seg' (which skips the surface pipeline
    entirely) never produces it -- same restriction _cortical_thickness_table
    has, and for the same reason.

    '% of Total Brain Volume' lets a reader sanity-check the numbers at a
    glance (grey + white should be well under 100% of BrainSeg, since BrainSeg
    also counts ventricles, CSF and subcortical structures neither total
    covers) without cross-referencing the raw mm3 values by hand."""
    measures = _parse_brainvol_stats(brainvol_stats_path)
    brain_volume_mm3 = measures.get('BrainSeg')

    labels, values_mm3 = [], []
    for label, key in GLOBAL_VOLUME_MEASURES:
        labels.append(label)
        values_mm3.append(measures.get(key))

    return pd.DataFrame({
        'Measure': labels,
        'Volume_mm3': [round(v, 2) if v is not None else None for v in values_mm3],
        'Volume_mL': [round(v / 1000, 2) if v is not None else None for v in values_mm3],
        '% of Total Brain Volume': [
            round(v / brain_volume_mm3 * 100, 1) if v is not None and brain_volume_mm3 else None
            for v in values_mm3
        ],
    })


def _thickness_color_range(surf_dir):
    """2nd/98th percentile of combined lh+rh thickness, so both hemispheres'
    renders share one color scale and a handful of outlier vertices don't
    wash it out."""
    lh = nib.freesurfer.read_morph_data(os.path.join(surf_dir, 'lh.thickness'))
    rh = nib.freesurfer.read_morph_data(os.path.join(surf_dir, 'rh.thickness'))
    combined = np.concatenate([lh, rh])
    return tuple(np.percentile(combined, [2, 98]))


def _thickness_surface_images(surf_dir, hemi, vmin, vmax, views=CORTICAL_THICKNESS_VIEWS):
    """Render hemi's pial surface reconstruction, color-coded by per-vertex
    cortical thickness, from each of `views`. Returns a list of RGB uint8
    arrays (one per view), ready for upload_images_as_sc()."""
    # Imported lazily: nilearn isn't installed in the currently-running
    # ohif_api container yet (only added to requirements.txt so far, image
    # not rebuilt), and a module-level import here would crash the whole API
    # on startup rather than just this one feature.
    from nilearn import plotting

    images = []
    for view in views:
        fig = plotting.plot_surf_stat_map(
            surf_mesh=os.path.join(surf_dir, f'{hemi}.pial'),
            stat_map=os.path.join(surf_dir, f'{hemi}.thickness'),
            bg_map=os.path.join(surf_dir, f'{hemi}.curv'),
            hemi=_HEMI_NAMES[hemi],
            view=view,
            bg_on_data=True,
            cmap='viridis',
            vmin=vmin,
            vmax=vmax,
            colorbar=True,
            title=f'{hemi}.pial cortical thickness -- {view}',
        )
        images.append(rasterize_figure(fig, mode='RGB'))
    return images


def _cortical_thickness_renders(surf_dir):
    """Render cortical thickness on both hemispheres' pial surfaces across
    the fixed view set. Returns a list of RGB uint8 arrays ready for upload
    as a DICOM Secondary Capture series."""
    vmin, vmax = _thickness_color_range(surf_dir)
    return (
        _thickness_surface_images(surf_dir, 'lh', vmin, vmax)
        + _thickness_surface_images(surf_dir, 'rh', vmin, vmax)
    )


def fastsurfer_core(input_dir, output_dir, mode='full', gpu_index=None, include_renders=True):
    """Run FastSurfer on an already-staged local NIfTI input and parse its
    outputs into local-file products, without touching dcm4chee.

    input_dir must contain a 't1n.nii.gz'. Output is written under
    output_dir. gpu_index pins the run to one GPU -- see
    _run_fastsurfer_container's docstring. Returns a dict:
      - 'subject_dir': FastSurfer's per-subject output directory
      - 'segmentation': resampled whole-brain segmentation array (t1 grid)
      - 'segmentation_source_path': path to FastSurfer's native-grid segmentation
      - 'segmentation_labels' / 'segmentation_values': for upload_segmentation()
      - 'volume_table': left/right subcortical + DKT volume comparison DataFrame
    mode='full' additionally includes:
      - 'thickness_table': left/right cortical thickness (plus its L-R
        asymmetry)/area/volume DataFrame
      - 'global_volume_table': total brain/grey/white matter volume DataFrame
        (see _global_volume_table) -- only computed here because it is read
        from brainvol.stats, a recon-surf byproduct mode='seg' never produces
      - 'cortical_thickness_renders': list of RGB uint8 pial surface renders,
        only when include_renders (default True) -- these are several
        matplotlib/nilearn surface renders (see _cortical_thickness_renders)
        and are the single slowest step in this function; the batch pipeline
        (routes.py's _run_fastsurfer_tool) never persists them (see
        save_fastsurfer_core_outputs), so it passes include_renders=False to
        skip the cost entirely rather than compute-and-discard them. The
        interactive per-study route (fastsurfer_wrapper below) does upload
        them, so it keeps the default.
    """
    os.makedirs(output_dir, exist_ok=True)
    sid = os.path.basename(os.path.normpath(input_dir))
    t1_nifti_path = os.path.join(input_dir, 't1n.nii.gz')

    gpu_index = choose_gpu_index(gpu_index)
    _run_fastsurfer_container(input_dir, output_dir, sid, mode=mode, gpu_index=gpu_index)

    subject_dir = os.path.join(output_dir, sid)
    stats_dir = os.path.join(subject_dir, 'stats')
    mri_dir = os.path.join(subject_dir, 'mri')

    seg_file, aseg_stats_path = _select_segmentation_and_stats(mri_dir, stats_dir)
    labels, values = _segmentation_labels_from_aseg_stats(aseg_stats_path)
    segmentation = _resample_segmentation_to_t1_grid(seg_file, t1_nifti_path)

    result = {
        'subject_dir': subject_dir,
        'segmentation': segmentation,
        'segmentation_source_path': seg_file,
        'segmentation_labels': labels,
        'segmentation_values': values,
        'volume_table': _hemisphere_volume_table(aseg_stats_path),
    }

    if mode == 'full':
        surf_dir = os.path.join(subject_dir, 'surf')
        _require_surface_outputs(subject_dir, stats_dir, surf_dir, include_renders)
        result['thickness_table'] = _cortical_thickness_table(stats_dir)
        result['global_volume_table'] = _global_volume_table(
            os.path.join(stats_dir, 'brainvol.stats')
        )
        if include_renders:
            result['cortical_thickness_renders'] = _cortical_thickness_renders(surf_dir)

    return result


def fastsurfer_batch_stats(result, mode):
    """Flatten fastsurfer_core()'s already-computed volume_table (and
    thickness_table, mode='full') into a flat {metric_name: value} dict for
    the batch pipeline's results_overview.csv (see routes.py's
    _run_fastsurfer_tool) -- reuses the stats fastsurfer_core() already
    produced rather than recomputing anything."""
    stats = {}
    for _, row in result['volume_table'].iterrows():
        region = row['Region']
        stats[f'{region}_Volume_mm3_Left'] = float(row['Volume_mm3_Left'])
        stats[f'{region}_Volume_mm3_Right'] = float(row['Volume_mm3_Right'])
        stats[f'{region}_Asymmetry_%'] = float(row['Asymmetry_%'])

    if mode == 'full':
        for _, row in result['thickness_table'].iterrows():
            region = row['Region']
            stats[f'{region}_ThickAvg_mm_Left'] = float(row['ThickAvg_Left'])
            stats[f'{region}_ThickAvg_mm_Right'] = float(row['ThickAvg_Right'])
            stats[f'{region}_ThickAvg_Asymmetry_%'] = float(row['ThickAvg_Asymmetry_%'])
            stats[f'{region}_SurfArea_mm2_Left'] = float(row['SurfArea_Left'])
            stats[f'{region}_SurfArea_mm2_Right'] = float(row['SurfArea_Right'])
            stats[f'{region}_GrayVol_mm3_Left'] = float(row['GrayVol_Left'])
            stats[f'{region}_GrayVol_mm3_Right'] = float(row['GrayVol_Right'])
        for _, row in result['global_volume_table'].iterrows():
            if row['Volume_mm3'] is not None:
                measure = row['Measure'].replace(' ', '_')
                stats[f'{measure}_mm3'] = float(row['Volume_mm3'])

    return stats


### ---------------------------------------------------------------------- ###
### batch pipeline: write core outputs to a plain output tree (no dcm4chee) ###
### ---------------------------------------------------------------------- ###

def save_fastsurfer_core_outputs(result, input_dir, mode, out_dir):
    """Write fastsurfer_core()'s in-memory outputs to `out_dir` as plain
    files, for the batch pipeline's ZIP-in/ZIP-out route (POST /batch/run,
    when 'fastsurfer' is one of the selected tools). No DICOM SEG here -- a
    batch case has no dcm4chee study to attach one to. Writes the
    whole-brain segmentation as a NIfTI on the T1's grid
    (`result['segmentation']` is already resampled to it) plus
    volume/thickness stats as CSV."""
    os.makedirs(out_dir, exist_ok=True)

    t1_affine = nib.load(os.path.join(input_dir, 't1n.nii.gz')).affine
    seg_img = nib.Nifti1Image(result['segmentation'].astype(np.int16), t1_affine)
    nib.save(seg_img, os.path.join(out_dir, 'aseg.nii.gz'))

    result['volume_table'].to_csv(os.path.join(out_dir, 'volume_table.csv'), index=False)
    if mode == 'full':
        result['thickness_table'].to_csv(os.path.join(out_dir, 'thickness_table.csv'), index=False)
        result['global_volume_table'].to_csv(
            os.path.join(out_dir, 'global_volume_table.csv'), index=False
        )


### ---------------------------------------------------------------------- ###
### wrapper: stage-in from dcm4chee -> core -> stage-out to dcm4chee       ###
### ---------------------------------------------------------------------- ###

def fastsurfer_wrapper(studyInstanceUID, mode='full'):
    """End-to-end FastSurfer run for a study: download the T1 series, run
    FastSurfer, upload the whole-brain segmentation as a DICOM SEG, and
    upload volume/thickness comparison tables (mode='full' also adds a
    global brain/grey/white matter volume summary and cortical-thickness
    surface renders). This is what the /fastsurfer/<mode>/<studyInstanceUID>
    route calls."""
    study_dir = os.path.join(FASTSURFER_STORAGE_ROOT, studyInstanceUID)
    output_dir = os.path.join(study_dir, 'output')

    # stage-in
    required_series = ['t1n']
    UID_dic = store_required_series_as_nifti(studyInstanceUID, required_series, study_dir)
    if not UID_dic or 't1n' not in UID_dic:
        raise ValueError(
            f"Study {studyInstanceUID} is missing a T1 series required for FastSurfer."
        )

    # core
    result = fastsurfer_core(study_dir, output_dir, mode=mode)

    # stage-out
    reference_instance = client.retrieve_series(
        study_instance_uid=studyInstanceUID, series_instance_uid=UID_dic['t1n']
    )[0]

    upload_segmentation(
        result['segmentation'],
        labels=result['segmentation_labels'], values=result['segmentation_values'],
        study_uid=studyInstanceUID,
        series_uid=UID_dic['t1n'],
        seg_description='FastSurfer whole-brain segmentation',
        seg_series_number=6000,
    )

    upload_dataframe_as_sc(
        result['volume_table'], title='FastSurfer: Left/Right Volume Comparison (mm3)',
        study_uid=studyInstanceUID, reference_instance=reference_instance,
        series_description='FastSurfer Volume L-R Comparison', series_number=6001,
        highlight_rows=_asymmetry_highlight(result['volume_table'], 'Asymmetry_%'),
        highlight_note=_highlight_note(),
    )

    if mode == 'full':
        upload_dataframe_as_sc(
            result['thickness_table'],
            title='FastSurfer: Cortical Thickness (L vs R + Asymmetry) / Area / Volume',
            study_uid=studyInstanceUID, reference_instance=reference_instance,
            series_description='FastSurfer Cortical Thickness L-R Comparison', series_number=6002,
            highlight_rows=_asymmetry_highlight(
                result['thickness_table'], 'ThickAvg_Asymmetry_%'
            ),
            highlight_note=_highlight_note(),
        )

        upload_dataframe_as_sc(
            result['global_volume_table'],
            title='FastSurfer: Global Brain Volumes',
            study_uid=studyInstanceUID, reference_instance=reference_instance,
            series_description='FastSurfer Global Brain Volumes', series_number=6004,
        )

        # render the pial surface color-coded by cortical thickness and upload
        # the renders as a Secondary Capture series (see
        # _thickness_surface_images() docstring for why this is snapshots
        # rather than an interactive DICOM mesh)
        upload_images_as_sc(
            result['cortical_thickness_renders'],
            study_uid=studyInstanceUID, reference_instance=reference_instance,
            series_description='FastSurfer Cortical Thickness Surface Renders',
            series_number=6003,
        )
