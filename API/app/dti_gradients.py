"""
Recovers the diffusion gradient scheme (b-values + gradient directions) of a
Siemens DTI series that came through the PACS with its private tags stripped.

The scanner writes b-value and gradient direction into Siemens private tags
and the CSA header. This project's PACS export drops both (the only private
tags surviving are (0013,1010) and (0013,1012)), so dcm2niix
emits a .bval and .bvec that are entirely zeros -- valid files, useless
content. dtifit run against those produces a plausible-looking but wrong FA
map, so this has to be caught and repaired before the tensor fit, not after.

Two things are recoverable and one is not:

  * b-values      -- recoverable. Which volumes are b=0 follows from the
                     signal level itself (b0 volumes are ~3x brighter), and
                     the non-zero b-value is still readable from a public
                     DICOM tag or the sequence name.
  * directions    -- recoverable *only* if the scanner's own derived FA/ADC/
                     ColFA maps came along in the export, because those give
                     the diffusion tensor per voxel and the directions can be
                     fitted against it (estimate_directions below).
  * the sign of g -- not recoverable, and not needed: the tensor is antipodally
                     symmetric, so g and -g fit identically.

The clean fix is an Enhanced-DICOM export straight from the scanner, where the
directions sit in the MRDiffusionSequence (0018,9117) -- this module is the
fallback for data that already went through the PACS. Ported from the
validated reference implementation in ../DTI/estimate_bvecs.py, generalised
from that script's hard-coded 30 directions to a direction count detected from
the data (see detect_direction_period).

Nothing here is trusted on its own: dti_functions.validate_against_scanner_fa()
re-fits the tensor with the reconstructed scheme and correlates the result
against the scanner's own FA map before the pipeline is allowed to continue.
"""
import os
import re

import nibabel as nib
import numpy as np

from .backend_helpers import log

# Below this, a reconstructed direction set is not trusted enough to build a
# tensor -- let alone tractography -- on. The reference datasets reached
# r = 0.996 and r = 0.995 against the scanner's FA on uncorrected data, so
# this is a floor for "something went wrong", not a target.
MIN_FA_CORRELATION = 0.90

# A gradient direction repeated across measurement repetitions produces
# near-identical volumes, so the correlation at the true period stands out
# sharply from the background correlation between different directions. This
# is the margin the winning period has to clear (see detect_direction_period).
_PERIOD_CORRELATION_MARGIN = 0.02

# b0 volumes are roughly 3x brighter than diffusion-weighted ones at b=1000.
# Anything below this ratio between the two brightness clusters means the
# series has no b0 volume at all, not that the split landed badly.
_MIN_B0_BRIGHTNESS_RATIO = 1.3


def gradients_are_usable(bvals, bvecs):
    """True if dcm2niix recovered a real gradient scheme, i.e. there is at
    least one non-zero b-value and the direction vectors are not all zero.

    Both halves matter: a PACS-stripped export yields all-zero bvecs *and*
    all-zero bvals, but a partially-stripped one could yield either alone,
    and each is equally fatal to dtifit.
    """
    return bool(np.any(bvals > 0)) and float(np.abs(bvecs).sum()) > 0


def load_gradient_files(bval_path, bvec_path):
    """Read a dcm2niix .bval/.bvec pair as (bvals[n], bvecs[3, n]).

    Returns (None, None) if either file is missing -- dcm2niix only writes
    them when it recognises the series as diffusion-weighted at all.
    """
    if not (os.path.exists(bval_path) and os.path.exists(bvec_path)):
        return None, None
    bvals = np.atleast_1d(np.loadtxt(bval_path))
    bvecs = np.atleast_2d(np.loadtxt(bvec_path))
    return bvals, bvecs


def nominal_b_values(instances):
    """The acquisition's non-zero b-values, from whichever public DICOM tag or
    sequence name still carries them, as a sorted tuple.

    Tried in order of trustworthiness:
      1. (0018,9087) DiffusionBValue -- the standard tag, present on
         enhanced/newer exports.
      2. (0019,100C) -- Siemens' own b-value tag. Usually stripped along with
         the rest of the private block, but free to check.
      3. (0018,0024) SequenceName -- what survived in this project's exports:
         "*ep_b0", "*ep_b1000#1" ... "#30". The direction index after '#' is
         useless here (it cannot be mapped back onto dcm2niix's volume order
         without assuming its sorting), but the b-value in front of it is not.

    Returned as a tuple rather than a single number so the caller can tell a
    single-shell acquisition from a multi-shell one. That distinction matters:
    classify_b0_volumes() below can only split volumes into b=0 and
    "diffusion-weighted", so on a stripped multi-shell export there would be
    no way to say which shell each volume belongs to, and assuming one would
    mis-scale every tensor. resolve_gradient_scheme() refuses that case.

    Empty if the export kept none of them.
    """
    b_values = set()
    for ds in instances:
        for tag in ((0x0018, 0x9087), (0x0019, 0x100C)):
            element = ds.get(tag)
            if element is not None and element.value not in (None, ''):
                try:
                    b_values.add(float(element.value))
                except (TypeError, ValueError):
                    pass

        match = re.search(r'_?b(\d+)', str(getattr(ds, 'SequenceName', '')))
        if match:
            b_values.add(float(match.group(1)))

    return tuple(sorted(b for b in b_values if b > 0))


def _brain_voxel_mask(volume):
    """Coarse brain mask from a single volume -- everything above half the
    mean of the brightest 10% of voxels. Only ever used to restrict the
    per-volume brightness and correlation statistics below to tissue; the real
    mask the pipeline runs on comes from BET on the averaged b0."""
    bright = np.percentile(volume, 90)
    return volume > 0.5 * bright


def classify_b0_volumes(data, required=True):
    """Split a 4D DWI array's volumes into b=0 and diffusion-weighted purely
    by signal level, returning a boolean mask over the volume axis.

    Used instead of the sequence names because it needs no assumption about
    how dcm2niix ordered the volumes relative to the instance order -- and it
    is exactly the check the reference protocol used to confirm the b-values
    it had derived from the sequence names in the first place.

    The two groups separate by a factor of ~3 at b=1000, so the split is the
    largest gap in the sorted per-volume mean brain signal. Groups that are
    not far enough apart mean the series holds no b0 volume at all -- eddy and
    the tensor fit both need one, and silently treating the dimmest
    diffusion-weighted volume as a b0 would corrupt everything downstream.
    That case raises by default; callers that can still supply a b0 from
    elsewhere (dti_functions._merge_external_b0, for the protocol versions
    that export the b0 as its own series) pass required=False and get None.
    """
    mask = _brain_voxel_mask(data[..., 0])
    means = np.array([data[..., i][mask].mean() for i in range(data.shape[-1])])

    order = np.argsort(means)
    sorted_means = means[order]
    gaps = np.diff(sorted_means)
    split = int(np.argmax(gaps))

    low = sorted_means[:split + 1]
    high = sorted_means[split + 1:]
    ratio = high.mean() / low.mean() if low.mean() > 0 else np.inf

    if ratio < _MIN_B0_BRIGHTNESS_RATIO:
        if not required:
            log(f"No b=0 volume in the raw series (all {len(means)} volumes "
                f"within a factor of {ratio:.2f} in brain signal).")
            return None
        raise ValueError(
            "This diffusion series contains no b=0 volume (all volumes are "
            f"within a factor of {ratio:.2f} in brain signal), and the study "
            "holds no separate b0 series to take one from either. eddy and "
            "dtifit both need one; re-export the series including its b0 "
            "volumes, or export from the scanner as enhanced DICOM."
        )

    is_b0 = np.zeros(len(means), dtype=bool)
    is_b0[order[split + 1:]] = True

    log(f"b0 volumes: {int(is_b0.sum())} of {len(means)} "
        f"(mean brain signal {high.mean():.0f} vs {low.mean():.0f}, ratio {ratio:.2f})")
    return is_b0


def detect_direction_period(data, dw_indices, sample_voxels=20000, seed=0):
    """Number of distinct gradient directions in the diffusion-weighted
    volumes, detected from the data.

    A Siemens multi-repetition DTI acquisition cycles the same direction table
    once per repetition, so volume i and volume i+N image the same direction
    and are near-identical, while volumes of different directions are not.
    Correlating every diffusion-weighted volume against the one N positions
    later therefore peaks sharply at the true table size.

    Detected rather than taken from the sequence name ("mddw_30") because the
    reference protocol's central finding was that two software versions ship
    *different* direction tables under the same sequence name -- so nothing
    about the direction scheme may be assumed from metadata, and a table
    reconstructed for one dataset must never be carried to another.

    Returns N; equal to len(dw_indices) when no repetition is found, i.e.
    every volume is its own direction.
    """
    n_dw = len(dw_indices)

    rng = np.random.default_rng(seed)
    mask = _brain_voxel_mask(data[..., dw_indices[0]])
    voxels = np.argwhere(mask)
    if len(voxels) > sample_voxels:
        voxels = voxels[rng.choice(len(voxels), sample_voxels, replace=False)]
    x, y, z = voxels.T

    # (n_dw, n_voxels), z-scored per volume so a plain dot product is the
    # correlation coefficient.
    samples = np.stack([data[x, y, z, i] for i in dw_indices]).astype(np.float32)
    samples -= samples.mean(axis=1, keepdims=True)
    samples /= np.linalg.norm(samples, axis=1, keepdims=True) + 1e-9
    correlation = samples @ samples.T

    off_diagonal = correlation[~np.eye(n_dw, dtype=bool)].mean()

    best_period, best_score = n_dw, -np.inf
    for period in range(1, n_dw // 2 + 1):
        if n_dw % period:
            continue  # a partial repetition would mean a truncated series
        score = np.mean([correlation[i, i + period] for i in range(n_dw - period)])
        if score > best_score:
            best_period, best_score = period, score

    if best_score < off_diagonal + _PERIOD_CORRELATION_MARGIN:
        log(f"No direction repetition detected ({n_dw} volumes, best period "
            f"correlation {best_score:.3f} vs baseline {off_diagonal:.3f}) -- "
            "treating every volume as its own direction.")
        return n_dw

    log(f"Direction table size {best_period} "
        f"({n_dw // best_period} repetitions; period correlation "
        f"{best_score:.3f} vs baseline {off_diagonal:.3f})")
    return best_period


def count_directions(bvals, bvecs, decimals=3):
    """Number of distinct gradient directions among the diffusion-weighted
    volumes in a resolved (bvals, bvecs) pair, however the scheme was
    obtained.

    Repeated measurements of the same direction -- one row per repetition of
    a Siemens multi-repetition acquisition, or an antipodal g vs -g, which a
    tensor fit cannot tell apart -- collapse to one, so this counts the
    direction *table* size rather than the diffusion-weighted volume count.
    That is what dti_functions._bedpostx_fibre_target() sizes bedpostx's
    fibre model against, whether the scheme came straight from the DICOM
    headers or was reconstructed above.
    """
    bvals = np.asarray(bvals)
    directions = np.asarray(bvecs)[:, bvals > 0].T
    if len(directions) == 0:
        return 0
    # Canonicalise the antipodal sign so g and -g count once, the same way
    # estimate_directions() fixes it before writing bvecs out.
    dominant = np.argmax(np.abs(directions), axis=1)
    sign = np.sign(directions[np.arange(len(directions)), dominant])
    sign[sign == 0] = 1
    canonical = np.round(directions * sign[:, None], decimals)
    return int(len(np.unique(canonical, axis=0)))


def _load_colfa(path):
    """Read a dcm2niix-converted ColFA map as a float (X, Y, Z, 3) array.

    dcm2niix writes the scanner's RGB ColFA series as a NIfTI with an RGB24
    datatype -- one packed 3-byte record per voxel, which nibabel surfaces as
    a structured 3D array rather than a 4D one. Older/derived exports
    sometimes arrive already split into a 4D volume instead, so both are
    accepted.
    """
    raw = np.asanyarray(nib.load(path).dataobj)
    if raw.ndim == 4 and raw.shape[-1] == 3:
        return raw.astype(np.float32)
    if raw.dtype.fields is not None or raw.ndim == 3:
        return raw.view((np.uint8, 3)).astype(np.float32)
    raise ValueError(f"{os.path.basename(path)} is not an RGB colour-FA map (shape {raw.shape}).")


def estimate_directions(dwi_path, bvals, scan_fa_path, scan_adc_path, scan_colfa_path,
                        n_directions=None, seed=0):
    """Fit the acquisition's gradient directions against the scanner's own
    derived tensor maps, and return them as a (3, n_volumes) bvec array.

    The scanner ships FA, ADC and ColFA alongside the raw series, and those
    three together define the diffusion tensor D in every voxel: ADC gives its
    mean diffusivity, FA its anisotropy, ColFA its principal eigenvector. For
    gradient direction g the measured signal obeys

        -ln(S_i / S_0) / b  =  g_i^T D g_i

    whose right-hand side is linear in the six independent components of
    G_i = g_i g_i^T -- so with ~10^5 masked voxels every direction is massively
    over-determined and solvable by least squares, with g_i recovered as the
    principal eigenvector of the fitted G_i.

    The complication is that ColFA encodes only the *magnitudes* of the
    eigenvector components: the signs of D's off-diagonal terms are lost.
    That is handled by alternating between fitting the directions and picking,
    per voxel, the best of the four sign variants, restarted from several
    initialisations to avoid a local optimum. The overall sign of g stays
    undetermined and does not matter (the tensor is antipodally symmetric).

    `n_directions` defaults to detecting the repetition period from the data
    (see detect_direction_period) and averaging the repeats of each direction
    before fitting, which is what makes the fit as noise-tolerant as it is.

    Returns (bvecs, n_directions, fit_rms). The fit's RMS residual says only
    how well the model closed on itself; whether the directions are *right* is
    decided by dti_functions.validate_against_scanner_fa(), which re-fits the
    tensor with them and correlates the resulting FA against the scanner's own
    map on the uncorrected data. (The reference protocol additionally
    re-estimated each measurement repetition separately and compared the
    angles, reaching a median of 0.6-1.7 degrees; that is a self-consistency
    check, and is not repeated here because the FA correlation tests the same
    reconstruction against an independent reference instead.)
    """
    dwi_img = nib.load(dwi_path)
    data = np.asanyarray(dwi_img.dataobj, dtype=np.float32)

    fa = nib.load(scan_fa_path).get_fdata() / 1000.0   # scanner stores FA x1000
    adc = nib.load(scan_adc_path).get_fdata()          # in 1e-6 mm^2/s
    rgb = _load_colfa(scan_colfa_path)

    for name, array in (('FA', fa), ('ADC', adc), ('ColFA', rgb)):
        if array.shape[:3] != data.shape[:3]:
            raise ValueError(
                f"The scanner's {name} map has shape {array.shape[:3]} but the raw "
                f"diffusion series has {data.shape[:3]} -- they are not from the "
                "same acquisition, so the directions cannot be fitted against them."
            )

    b0_indices = np.where(bvals == 0)[0]
    dw_indices = np.where(bvals > 0)[0]
    s0 = data[..., b0_indices].mean(axis=3)

    if n_directions is None:
        n_directions = detect_direction_period(data, dw_indices)
    if len(dw_indices) % n_directions:
        raise ValueError(
            f"{len(dw_indices)} diffusion-weighted volumes do not divide into "
            f"{n_directions} directions."
        )

    # Volumes of the same direction, averaged over the repetitions. The
    # direction table is cycled once per repetition, so they sit n_directions
    # apart in the volume order.
    groups = [dw_indices[i::n_directions] for i in range(n_directions)]
    signal = np.stack([data[..., group].mean(axis=3) for group in groups], axis=-1)

    # Well-conditioned white matter only: enough b0 signal to take a log ratio
    # of, a physiological diffusivity, and enough anisotropy that the principal
    # eigenvector ColFA reports is actually defined.
    mask = ((s0 > 60) & (adc > 200) & (adc < 1600) & (fa > 0.15)
            & (signal.min(axis=-1) > 5))
    n_voxels = int(mask.sum())
    if n_voxels < 5000:
        raise ValueError(
            f"Only {n_voxels} voxels pass the white-matter criteria for fitting "
            "gradient directions -- too few to fit against. Check that the "
            "scanner's FA/ADC/ColFA maps belong to this diffusion series."
        )
    log(f"Fitting {n_directions} gradient directions against {n_voxels} white-matter voxels")

    # Rebuild each masked voxel's tensor from the scanner's maps: eigenvalues
    # from (FA, MD) assuming a prolate tensor, eigenvector from ColFA.
    md = adc[mask] / 1000.0
    fa_masked = np.clip(fa[mask], 0, 0.95)
    eigenvector = rgb[mask]
    eigenvector /= (np.linalg.norm(eigenvector, axis=1, keepdims=True) + 1e-9)
    ratio = fa_masked / np.sqrt(np.maximum(3 - 2 * fa_masked ** 2, 1e-6))
    axial, radial = md * (1 + 2 * ratio), md * (1 - ratio)
    delta = axial - radial

    # y[v, i] = -ln(S_iv / S0_v) / b, up to the constant 1/b that the fit's
    # intercept column absorbs.
    y = -np.log(np.maximum(signal[mask], 1e-3) / np.maximum(s0[mask], 1e-3)[:, None])

    sign_variants = np.array([[1, 1, 1], [1, 1, -1], [1, -1, 1], [1, -1, -1]], np.float32)

    def tensor_columns(selection):
        """The six independent components of D per voxel, under one sign
        choice per voxel for ColFA's lost off-diagonal signs."""
        v = eigenvector * sign_variants[selection]
        columns = np.empty((n_voxels, 6), np.float32)
        columns[:, 0] = radial + delta * v[:, 0] ** 2
        columns[:, 1] = radial + delta * v[:, 1] ** 2
        columns[:, 2] = radial + delta * v[:, 2] ** 2
        columns[:, 3] = delta * v[:, 0] * v[:, 1]
        columns[:, 4] = delta * v[:, 0] * v[:, 2]
        columns[:, 5] = delta * v[:, 1] * v[:, 2]
        return columns

    def design_matrix(columns):
        # Off-diagonal terms appear twice in g^T D g, hence the factor 2; the
        # trailing ones column is the per-direction intercept (the 1/b scale
        # and any residual signal offset).
        return np.concatenate(
            [columns[:, :3], 2 * columns[:, 3:], np.ones((n_voxels, 1), np.float32)], axis=1
        )

    rng = np.random.default_rng(seed)
    best_rms, best_coefficients = np.inf, None

    for init in range(6):
        # Init 0 is the all-positive guess, which is the one that won on both
        # reference datasets; the random restarts guard against it being a
        # local optimum on a different acquisition.
        selection = (np.zeros(n_voxels, np.int64) if init == 0
                     else rng.integers(0, 4, n_voxels))
        previous_rms, rms = np.inf, np.inf

        for iteration in range(25):
            coefficients = np.linalg.lstsq(design_matrix(tensor_columns(selection)), y,
                                           rcond=None)[0]
            residuals = np.empty((n_voxels, 4), np.float32)
            for s in range(4):
                matrix = design_matrix(tensor_columns(np.full(n_voxels, s)))
                residuals[:, s] = ((matrix @ coefficients - y) ** 2).sum(axis=1)
            selection = residuals.argmin(axis=1)
            rms = float(np.sqrt(residuals.min(axis=1).mean() / n_directions))
            if abs(previous_rms - rms) < 1e-7:
                break
            previous_rms = rms

        coefficients = np.linalg.lstsq(design_matrix(tensor_columns(selection)), y,
                                       rcond=None)[0]
        log(f"  init {init}: RMS {rms:.6f} ({iteration + 1} iterations)")
        if rms < best_rms:
            best_rms, best_coefficients = rms, coefficients.copy()

    log(f"Best gradient-direction fit: RMS {best_rms:.6f}")

    # Each direction's fitted G = g g^T; g is its principal eigenvector.
    fitted = best_coefficients[:6].T
    directions = np.zeros((n_directions, 3))
    for i in range(n_directions):
        g_outer = np.array([
            [fitted[i, 0], fitted[i, 3], fitted[i, 4]],
            [fitted[i, 3], fitted[i, 1], fitted[i, 5]],
            [fitted[i, 4], fitted[i, 5], fitted[i, 2]],
        ])
        g = np.linalg.eigh(g_outer)[1][:, -1]
        g /= np.linalg.norm(g)
        # Sign is undetermined; fix it deterministically so repeated runs on
        # the same data produce byte-identical bvec files.
        if g[np.argmax(np.abs(g))] < 0:
            g = -g
        directions[i] = g

    bvecs = np.zeros((len(bvals), 3))
    for i, group in enumerate(groups):
        bvecs[group] = directions[i]
    return bvecs.T, n_directions, best_rms
