"""
Deterministic whole-brain fibre tracking on top of this project's DTI tensor
fit, for the "cool 3D fibre picture" the FA/MD/tract-overlay series can't
give -- those are all 2D slices with colour standing in for orientation, not
actual fibre geometry (see dti_functions.py's tractography quality table:
"streamlines are model output, not axons; not for operative planning").

Deliberately does not reuse bedpostx/XTRACT's probabilistic pipeline. Getting
FSL's own streamlines would mean running probtrackx2 with --savepaths
per tract, reimplementing XTRACT's seed/waypoint/exclusion masks outside of
XTRACT itself for no clinical benefit -- this is for a picture, not a
reconstruction FSL hasn't already validated. Instead this runs the classic
FACT algorithm (Mori et al. 1999: step along the tensor's principal
eigenvector, stop on low FA or a sharp turn) directly on dtifit's own V1/FA
output, which dti_functions.py has already fitted and validated
(validate_against_scanner_fa) before this ever runs. No FSL call, no GPU.

Two things make this cheap in Python:
  * every streamline is stepped in a single batched numpy operation per
    iteration (see _track_batch) rather than one streamline at a time, so a
    few thousand streamlines cost a few hundred vectorised iterations, not
    a few hundred thousand;
  * V1's sign is undefined per voxel (dtifit reports an axis, not a
    direction -- the same ambiguity estimate_directions() resolves for
    ColFA), so trilinearly blending neighbouring voxels' raw vectors can
    cancel across a sign flip and kill a streamline for no physical reason.
    _interpolate_direction() re-signs each of the 8 corners against the
    streamline's own running direction before blending, the same fix
    estimate_directions() applies to ColFA's lost signs.
"""
import base64
import html as html_module

import matplotlib
matplotlib.use('Agg')  # headless server; must be set before pyplot is imported
import matplotlib.pyplot as plt
import numpy as np
from mpl_toolkits.mplot3d.art3d import Line3DCollection


def _world_to_voxel(points_world, inv_affine):
    homogeneous = np.concatenate([points_world, np.ones((len(points_world), 1))], axis=1)
    return (homogeneous @ inv_affine.T)[:, :3]


def _voxel_to_world(points_voxel, affine):
    homogeneous = np.concatenate([points_voxel, np.ones((len(points_voxel), 1))], axis=1)
    return (homogeneous @ affine.T)[:, :3]


def _trilinear(field, points):
    """Trilinear interpolation of a scalar (X,Y,Z) or vector (X,Y,Z,C) field
    at continuous voxel coordinates (K,3). Points that fall outside the
    volume come back marked invalid rather than clamped, so a streamline
    that leaves the field is stopped, not smeared along its last voxel."""
    shape = np.array(field.shape[:3])
    x, y, z = points[:, 0], points[:, 1], points[:, 2]
    valid = ((x >= 0) & (y >= 0) & (z >= 0)
             & (x <= shape[0] - 1) & (y <= shape[1] - 1) & (z <= shape[2] - 1))

    xc = np.clip(x, 0, shape[0] - 1.001)
    yc = np.clip(y, 0, shape[1] - 1.001)
    zc = np.clip(z, 0, shape[2] - 1.001)
    x0, y0, z0 = np.floor(xc).astype(np.intp), np.floor(yc).astype(np.intp), np.floor(zc).astype(np.intp)
    x1, y1, z1 = x0 + 1, y0 + 1, z0 + 1
    fx, fy, fz = xc - x0, yc - y0, zc - z0
    if field.ndim == 4:
        fx, fy, fz = fx[:, None], fy[:, None], fz[:, None]

    c000, c100 = field[x0, y0, z0], field[x1, y0, z0]
    c010, c110 = field[x0, y1, z0], field[x1, y1, z0]
    c001, c101 = field[x0, y0, z1], field[x1, y0, z1]
    c011, c111 = field[x0, y1, z1], field[x1, y1, z1]

    c00 = c000 * (1 - fx) + c100 * fx
    c10 = c010 * (1 - fx) + c110 * fx
    c01 = c001 * (1 - fx) + c101 * fx
    c11 = c011 * (1 - fx) + c111 * fx
    c0 = c00 * (1 - fy) + c10 * fy
    c1 = c01 * (1 - fy) + c11 * fy
    return c0 * (1 - fz) + c1 * fz, valid


def _interpolate_direction(v1, points, reference):
    """Trilinear interpolation of the tensor's principal eigenvector field,
    with each of the 8 corner vectors re-signed to agree with `reference`
    (the streamline's current direction) before blending. Without this, two
    neighbouring voxels reporting the same physical axis with opposite signs
    -- which dtifit makes no promise against -- partially cancel instead of
    reinforcing, and the interpolated vector can collapse toward zero right
    where the streamline needs it most. Returns a unit vector and a validity
    mask (false where the field has no data or the corner vectors disagree
    enough that the blend isn't trustworthy)."""
    shape = np.array(v1.shape[:3])
    x, y, z = points[:, 0], points[:, 1], points[:, 2]
    valid = ((x >= 0) & (y >= 0) & (z >= 0)
             & (x <= shape[0] - 1) & (y <= shape[1] - 1) & (z <= shape[2] - 1))

    xc = np.clip(x, 0, shape[0] - 1.001)
    yc = np.clip(y, 0, shape[1] - 1.001)
    zc = np.clip(z, 0, shape[2] - 1.001)
    x0, y0, z0 = np.floor(xc).astype(np.intp), np.floor(yc).astype(np.intp), np.floor(zc).astype(np.intp)
    x1, y1, z1 = x0 + 1, y0 + 1, z0 + 1
    fx, fy, fz = (xc - x0)[:, None], (yc - y0)[:, None], (zc - z0)[:, None]

    def corner(xi, yi, zi):
        vectors = v1[xi, yi, zi]
        sign = np.sign(np.sum(vectors * reference, axis=1, keepdims=True))
        sign[sign == 0] = 1.0
        return vectors * sign

    c000, c100 = corner(x0, y0, z0), corner(x1, y0, z0)
    c010, c110 = corner(x0, y1, z0), corner(x1, y1, z0)
    c001, c101 = corner(x0, y0, z1), corner(x1, y0, z1)
    c011, c111 = corner(x0, y1, z1), corner(x1, y1, z1)

    c00 = c000 * (1 - fx) + c100 * fx
    c10 = c010 * (1 - fx) + c110 * fx
    c01 = c001 * (1 - fx) + c101 * fx
    c11 = c011 * (1 - fx) + c111 * fx
    c0 = c00 * (1 - fy) + c10 * fy
    c1 = c01 * (1 - fy) + c11 * fy
    blended = c0 * (1 - fz) + c1 * fz

    norm = np.linalg.norm(blended, axis=1, keepdims=True)
    valid = valid & (norm[:, 0] > 1e-6)
    norm[norm == 0] = 1.0
    return blended / norm, valid


def _track_batch(v1, fa, affine, seeds_world, directions0_world, *,
                 step_mm, fa_stop, max_angle_deg, max_steps):
    """FACT integration of every streamline in the batch at once. Stepping
    all K streamlines together per iteration (rather than looping K times
    over max_steps) is what keeps a few thousand streamlines to a few
    hundred numpy operations total.

    A streamline that stops keeps repeating its last position in `history`
    for the remaining iterations (cheaper than reshaping the batch each
    time) and is trimmed back to its real length by the caller.
    """
    inv_affine = np.linalg.inv(affine)
    n_seeds = len(seeds_world)
    positions = seeds_world.astype(np.float64).copy()
    directions = directions0_world.astype(np.float64).copy()
    active = np.ones(n_seeds, dtype=bool)
    lengths = np.ones(n_seeds, dtype=np.int64)
    history = np.empty((max_steps + 1, n_seeds, 3), dtype=np.float32)
    history[0] = positions
    cos_limit = np.cos(np.radians(max_angle_deg))
    last_step = 0

    for step in range(1, max_steps + 1):
        last_step = step
        if not active.any():
            break
        voxel = _world_to_voxel(positions, inv_affine)
        fa_val, fa_valid = _trilinear(fa, voxel)
        new_dir, dir_valid = _interpolate_direction(v1, voxel, directions)

        turn_cos = np.sum(new_dir * directions, axis=1)
        keep = active & fa_valid & dir_valid & (fa_val >= fa_stop) & (turn_cos >= cos_limit)

        directions = np.where(keep[:, None], new_dir, directions)
        positions = np.where(keep[:, None], positions + step_mm * new_dir, positions)
        lengths[keep] = step + 1
        active = keep
        history[step] = positions

    history = history[:last_step + 1]
    return [history[:lengths[k], k, :] for k in range(n_seeds)]


def generate_whole_brain_streamlines(fa, v1, mask, affine, *,
                                     seed_count=6000, seed_fa_threshold=0.25,
                                     step_mm=1.0, fa_stop_threshold=0.15,
                                     max_angle_deg=60.0, max_length_mm=250.0,
                                     min_length_mm=20.0, seed=0):
    """Whole-brain deterministic streamlines from a fitted tensor, seeded
    wherever FA clears `seed_fa_threshold` and the brain mask holds, jittered
    within their voxel so the result doesn't look grid-locked. Each seed is
    tracked in both directions from its own (arbitrarily-signed) principal
    eigenvector and the two halves stitched into one line.

    `seed_count` bounds the *seed* count, not the final streamline count --
    short ones (under `min_length_mm`, usually a seed that landed right at a
    crossing or the mask edge) are dropped, so fewer lines come back than
    were seeded. Defaults aim at a few thousand streamlines: enough for a
    dense whole-brain picture, small enough that a browser can hold and
    animate the result.

    Returns a list of (N, 3) float32 arrays, each a streamline's points in
    the tensor's own world (mm) space -- the same affine dti_FA.nii.gz etc.
    carry, not the DICOM-upload layout _match_source_layout() produces.
    """
    rng = np.random.default_rng(seed)
    candidates = np.argwhere((fa > seed_fa_threshold) & (mask > 0))
    if len(candidates) == 0:
        return []

    n = min(seed_count, len(candidates))
    chosen = candidates[rng.choice(len(candidates), n, replace=False)].astype(np.float64)
    chosen += rng.uniform(-0.5, 0.5, size=chosen.shape)

    initial_dir, valid0 = _trilinear(v1, chosen)
    norm0 = np.linalg.norm(initial_dir, axis=1, keepdims=True)
    norm0[norm0 == 0] = 1.0
    initial_dir = initial_dir / norm0
    chosen, initial_dir = chosen[valid0], initial_dir[valid0]
    if len(chosen) == 0:
        return []

    seeds_world = _voxel_to_world(chosen, affine)
    max_steps = max(1, int(max_length_mm / step_mm))

    forward = _track_batch(v1, fa, affine, seeds_world, initial_dir,
                           step_mm=step_mm, fa_stop=fa_stop_threshold,
                           max_angle_deg=max_angle_deg, max_steps=max_steps)
    backward = _track_batch(v1, fa, affine, seeds_world, -initial_dir,
                            step_mm=step_mm, fa_stop=fa_stop_threshold,
                            max_angle_deg=max_angle_deg, max_steps=max_steps)

    streamlines = []
    for fwd, bwd in zip(forward, backward):
        line = np.concatenate([bwd[::-1], fwd[1:]], axis=0)
        if len(line) < 2:
            continue
        length = float(np.linalg.norm(np.diff(line, axis=0), axis=1).sum())
        if length >= min_length_mm:
            streamlines.append(line.astype(np.float32))
    return streamlines


### ---------------------------------------------------------------------- ###
### glass-brain PNG views, for the DICOM series upload                     ###
### ---------------------------------------------------------------------- ###

def _direction_colours(line):
    """Per-segment RGB from the segment's own tangent direction -- the same
    |direction| convention dti_functions.colour_fa() uses for the 2D colour
    FA map, so a fibre reads the same red/green/blue here as it does there."""
    segment_vectors = np.diff(line, axis=0)
    norms = np.linalg.norm(segment_vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return np.abs(segment_vectors) / norms


def render_glass_brain_views(streamlines, views, image_px=900,
                             background=(0.027, 0.031, 0.051),
                             max_streamlines=4000, alpha=0.2, seed=0):
    """One RGB uint8 array per (elev, azim) pair in `views`, each a dark-field
    render of (a random subset of, if there are more than max_streamlines)
    the streamlines, coloured by local direction. Callers montage these the
    same way the tensor stage montages its QC slices.

    alpha=0.2 (not opaque) is what makes it a *glass* brain: a bundle only a
    few streamlines deep stays faint while a dense crossing of many
    overlapping, semi-transparent lines builds up to bright -- the overlap
    itself carries density information a solid line wouldn't.
    """
    if not streamlines:
        return []

    rng = np.random.default_rng(seed)
    if len(streamlines) > max_streamlines:
        keep = rng.choice(len(streamlines), max_streamlines, replace=False)
        streamlines = [streamlines[i] for i in keep]

    all_points = np.concatenate(streamlines, axis=0)
    centre = all_points.mean(axis=0)
    radius = float(np.percentile(np.linalg.norm(all_points - centre, axis=1), 99)) or 1.0

    segments, colours = [], []
    for line in streamlines:
        if len(line) < 2:
            continue
        segments.append(np.stack([line[:-1], line[1:]], axis=1))
        colours.append(_direction_colours(line))
    segments = np.concatenate(segments, axis=0)
    colours = np.concatenate(colours, axis=0)

    images = []
    for elev, azim in views:
        fig = plt.figure(figsize=(image_px / 100, image_px / 100), dpi=100)
        fig.patch.set_facecolor(background)
        ax = fig.add_subplot(111, projection='3d')
        ax.set_facecolor(background)
        ax.add_collection3d(Line3DCollection(segments, colors=colours, linewidths=0.5, alpha=alpha))
        ax.set_xlim(centre[0] - radius, centre[0] + radius)
        ax.set_ylim(centre[1] - radius, centre[1] + radius)
        ax.set_zlim(centre[2] - radius, centre[2] + radius)
        ax.set_box_aspect((1, 1, 1))
        ax.view_init(elev=elev, azim=azim)
        ax.set_axis_off()
        fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
        fig.canvas.draw()
        buffer = np.asarray(fig.canvas.buffer_rgba())
        images.append(np.ascontiguousarray(buffer[..., :3]))
        plt.close(fig)
    return images


### ---------------------------------------------------------------------- ###
### 3D Slicer / ParaView / MI-Brain export                                 ###
### ---------------------------------------------------------------------- ###

def _direction_colours_per_point(line):
    """One RGB triple per point (rather than per segment, see
    _direction_colours) -- legacy VTK's POINT_DATA needs exactly one value
    per point. An endpoint borrows its one adjacent segment's colour; an
    interior point takes the segment leading into it."""
    if len(line) < 2:
        return np.zeros((len(line), 3))
    segment_colours = _direction_colours(line)
    point_colours = np.empty((len(line), 3))
    point_colours[1:] = segment_colours
    point_colours[0] = segment_colours[0]
    return point_colours


def write_streamline_vtk(streamlines, out_path):
    """Write streamlines as a legacy VTK PolyData file (ASCII), coloured by
    local direction -- the same |direction| convention as everywhere else in
    this module. Needs no library to write or read: 3D Slicer's core Models
    module opens it directly, and if the SlicerDMRI extension is installed
    it can instead be loaded as a Fiber Bundle for tract-specific display.

    Points are written in the tensor's own world-mm space (the same affine
    dti_FA.nii.gz etc. carry), which NIfTI treats as RAS -- Slicer's own
    scene coordinate convention -- so loading this alongside those NIfTIs in
    one scene overlays correctly with no extra transform. This is *not* the
    DICOM-upload layout _match_source_layout() produces; that exists to
    match a DICOM series' row/column convention for a 2D stack viewport,
    which is irrelevant to a 3D scene.
    """
    if not streamlines:
        raise ValueError("No streamlines to write.")

    total_points = sum(len(line) for line in streamlines)
    lines = [
        "# vtk DataFile Version 3.0",
        "DTI whole-brain streamlines (FACT tractography on dtifit's V1/FA)",
        "ASCII",
        "DATASET POLYDATA",
        f"POINTS {total_points} float",
    ]
    for line in streamlines:
        for point in line:
            lines.append(f"{point[0]:.3f} {point[1]:.3f} {point[2]:.3f}")

    connectivity_size = total_points + len(streamlines)
    lines.append(f"LINES {len(streamlines)} {connectivity_size}")
    offset = 0
    for line in streamlines:
        indices = " ".join(str(offset + i) for i in range(len(line)))
        lines.append(f"{len(line)} {indices}")
        offset += len(line)

    lines.append(f"POINT_DATA {total_points}")
    lines.append("COLOR_SCALARS Colors 3")
    for line in streamlines:
        for colour in _direction_colours_per_point(line):
            lines.append(f"{colour[0]:.4f} {colour[1]:.4f} {colour[2]:.4f}")

    with open(out_path, 'w') as handle:
        handle.write("\n".join(lines) + "\n")
    return out_path


### ---------------------------------------------------------------------- ###
### interactive WebGL viewer                                               ###
### ---------------------------------------------------------------------- ###

def _quantise(streamlines):
    """Pack every streamline's points into one int16 buffer (a shared
    centre/scale keeps every line on the same scale) plus a per-streamline
    point count, so the page embeds a compact blob and expands it into GPU
    vertices client-side rather than shipping float32 text/JSON."""
    all_points = np.concatenate(streamlines, axis=0)
    centre = all_points.mean(axis=0).astype(np.float32)
    half_extent = float(np.abs(all_points - centre).max()) or 1.0
    scale = 32000.0 / half_extent

    quantised = np.round((all_points - centre) * scale).astype(np.int16)
    lengths = np.array([len(line) for line in streamlines], dtype=np.uint16)
    return quantised, lengths, centre, scale


def _base64(array):
    return base64.b64encode(array.tobytes()).decode('ascii')


_VIEWER_TEMPLATE = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@600;800&family=Instrument+Sans:wght@400;500&family=JetBrains+Mono:wght@400;500&display=swap" />
<style>
  :root {
    --void: #07080d;
    --panel: rgba(16, 19, 28, 0.72);
    --bone: #e7e3da;
    --dim: #8b8f9c;
    --phosphor: #6fd9c9;
    --hairline: rgba(231, 227, 218, 0.14);
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden;
    background: var(--void); color: var(--bone);
    font-family: 'Instrument Sans', -apple-system, 'Segoe UI', sans-serif;
  }
  canvas { position: fixed; inset: 0; width: 100%; height: 100%; display: block; cursor: grab; }
  canvas:active { cursor: grabbing; }
  .hud {
    position: fixed; top: 1.5rem; left: 1.5rem; max-width: 21rem;
    background: var(--panel); border: 1px solid var(--hairline);
    backdrop-filter: blur(10px); padding: 1.1rem 1.3rem 1.3rem;
    pointer-events: none; user-select: none;
  }
  .hud h1 {
    margin: 0 0 0.15rem; font-family: 'Big Shoulders Display', sans-serif;
    font-weight: 800; font-size: 1.9rem; line-height: 0.95;
    letter-spacing: 0.01em; text-transform: uppercase; text-wrap: balance;
  }
  .hud .sub {
    margin: 0 0 0.8rem; font-size: 0.78rem; color: var(--dim);
    letter-spacing: 0.03em;
  }
  .hud .hint {
    font-size: 0.78rem; color: var(--bone); opacity: 0.75;
    border-top: 1px solid var(--hairline); padding-top: 0.7rem; margin-top: 0.1rem;
  }
  .stats {
    position: fixed; bottom: 1.5rem; left: 1.5rem;
    font-family: 'JetBrains Mono', monospace; font-size: 0.72rem;
    color: var(--dim); letter-spacing: 0.02em; line-height: 1.6;
    pointer-events: none; user-select: none;
  }
  .stats b { color: var(--phosphor); font-weight: 500; }
  .badge {
    position: fixed; bottom: 1.5rem; right: 1.5rem;
    font-family: 'JetBrains Mono', monospace; font-size: 0.68rem;
    color: var(--dim); opacity: 0.6; letter-spacing: 0.02em;
    pointer-events: none; user-select: none;
  }
  @media (prefers-reduced-motion: reduce) {
    .hud, .stats, .badge { transition: none !important; }
  }
</style>
</head>
<body>
<canvas id="gl"></canvas>
<div class="hud">
  <h1>__TITLE__</h1>
  <div class="sub">__SUBTITLE__</div>
  <div class="hint">drag to rotate &middot; scroll to zoom</div>
</div>
<div class="stats">
  <b>__N_STREAMLINES__</b> streamlines<br />
  <b>__N_DIRECTIONS__</b> measured directions<br />
  seed FA &gt; <b>__SEED_FA__</b>
</div>
<div class="badge">FSL dtifit &middot; FACT tractography</div>
<script>
(function () {
  "use strict";
  var POINTS_B64 = "__POINTS_B64__";
  var LENGTHS_B64 = "__LENGTHS_B64__";
  var CENTRE = [__CENTRE_X__, __CENTRE_Y__, __CENTRE_Z__];
  var SCALE = __SCALE__;

  function decode(b64, TypedArray) {
    var binary = atob(b64);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new TypedArray(bytes.buffer);
  }

  var points = decode(POINTS_B64, Int16Array);   // x,y,z per point, quantised
  var lengths = decode(LENGTHS_B64, Uint16Array); // points per streamline

  // Expand into a GL_LINES vertex buffer: position + colour + arc-length
  // fraction (0..1 along its own streamline, used to animate the grow-in)
  // per vertex, two vertices per segment.
  var segmentCount = 0;
  for (var s = 0; s < lengths.length; s++) segmentCount += Math.max(0, lengths[s] - 1);

  var positions = new Float32Array(segmentCount * 2 * 3);
  var colours = new Float32Array(segmentCount * 2 * 3);
  var progress = new Float32Array(segmentCount * 2);

  var pi = 0, vi = 0, ci = 0, gi = 0;
  for (s = 0; s < lengths.length; s++) {
    var n = lengths[s];
    var base = pi;
    var lineLength = 0;
    var segLen = new Float32Array(Math.max(0, n - 1));
    for (var k = 0; k < n - 1; k++) {
      var ax = points[(base + k) * 3], ay = points[(base + k) * 3 + 1], az = points[(base + k) * 3 + 2];
      var bx = points[(base + k + 1) * 3], by = points[(base + k + 1) * 3 + 1], bz = points[(base + k + 1) * 3 + 2];
      var dx = bx - ax, dy = by - ay, dz = bz - az;
      segLen[k] = Math.sqrt(dx * dx + dy * dy + dz * dz);
      lineLength += segLen[k];
    }
    var travelled = 0;
    for (k = 0; k < n - 1; k++) {
      var i0 = base + k, i1 = base + k + 1;
      var x0 = points[i0 * 3] / SCALE, y0 = points[i0 * 3 + 1] / SCALE, z0 = points[i0 * 3 + 2] / SCALE;
      var x1 = points[i1 * 3] / SCALE, y1 = points[i1 * 3 + 1] / SCALE, z1 = points[i1 * 3 + 2] / SCALE;
      var ddx = x1 - x0, ddy = y1 - y0, ddz = z1 - z0;
      var segNorm = Math.sqrt(ddx * ddx + ddy * ddy + ddz * ddz) || 1.0;
      var r = Math.min(1, Math.abs(ddx) / segNorm);
      var g = Math.min(1, Math.abs(ddy) / segNorm);
      var b = Math.min(1, Math.abs(ddz) / segNorm);

      var t0 = lineLength > 0 ? travelled / lineLength : 0;
      travelled += segLen[k];
      var t1 = lineLength > 0 ? travelled / lineLength : 1;

      positions[vi++] = x0; positions[vi++] = y0; positions[vi++] = z0;
      positions[vi++] = x1; positions[vi++] = y1; positions[vi++] = z1;
      colours[ci++] = r; colours[ci++] = g; colours[ci++] = b;
      colours[ci++] = r; colours[ci++] = g; colours[ci++] = b;
      progress[gi++] = t0; progress[gi++] = t1;
    }
    pi += n;
  }

  // --- minimal hand-rolled mat4 (no library: WebGL's CSP-safe here, a CDN isn't) ---
  function mat4Identity() { return new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]); }
  function mat4Multiply(a, b) {
    var out = new Float32Array(16);
    for (var col = 0; col < 4; col++) {
      for (var row = 0; row < 4; row++) {
        var sum = 0;
        for (var i = 0; i < 4; i++) sum += a[i * 4 + row] * b[col * 4 + i];
        out[col * 4 + row] = sum;
      }
    }
    return out;
  }
  function mat4Perspective(fovYRad, aspect, near, far) {
    var f = 1.0 / Math.tan(fovYRad / 2), out = new Float32Array(16);
    out[0] = f / aspect; out[5] = f; out[10] = (far + near) / (near - far); out[11] = -1;
    out[14] = (2 * far * near) / (near - far);
    return out;
  }
  function mat4Translate(x, y, z) {
    var m = mat4Identity(); m[12] = x; m[13] = y; m[14] = z; return m;
  }
  function mat4RotateX(rad) {
    var c = Math.cos(rad), s = Math.sin(rad), m = mat4Identity();
    m[5] = c; m[6] = s; m[9] = -s; m[10] = c; return m;
  }
  function mat4RotateY(rad) {
    var c = Math.cos(rad), s = Math.sin(rad), m = mat4Identity();
    m[0] = c; m[2] = -s; m[8] = s; m[10] = c; return m;
  }

  var canvas = document.getElementById('gl');
  var gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
  if (!gl) {
    document.body.innerHTML = '<div style="padding:2rem;font-family:monospace;color:#e7e3da">' +
      'WebGL is not available in this browser.</div>';
    return;
  }

  var vertexSrc = [
    'attribute vec3 aPosition;',
    'attribute vec3 aColour;',
    'attribute float aProgress;',
    'uniform mat4 uModelViewProjection;',
    'uniform float uReveal;',
    'varying vec3 vColour;',
    'varying float vHide;',
    'void main() {',
    '  vColour = aColour;',
    '  vHide = step(uReveal, aProgress);',
    '  gl_Position = uModelViewProjection * vec4(aPosition, 1.0);',
    '}'
  ].join('\n');

  var fragmentSrc = [
    'precision mediump float;',
    'varying vec3 vColour;',
    'varying float vHide;',
    'void main() {',
    '  if (vHide > 0.5) discard;',
    '  gl_FragColor = vec4(vColour, 0.72);',
    '}'
  ].join('\n');

  function compile(type, src) {
    var shader = gl.createShader(type);
    gl.shaderSource(shader, src);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      throw new Error(gl.getShaderInfoLog(shader));
    }
    return shader;
  }

  var program = gl.createProgram();
  gl.attachShader(program, compile(gl.VERTEX_SHADER, vertexSrc));
  gl.attachShader(program, compile(gl.FRAGMENT_SHADER, fragmentSrc));
  gl.linkProgram(program);
  gl.useProgram(program);

  function makeBuffer(data) {
    var buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    return buffer;
  }

  var positionBuffer = makeBuffer(positions);
  var aPosition = gl.getAttribLocation(program, 'aPosition');
  gl.enableVertexAttribArray(aPosition);

  var colourBuffer = makeBuffer(colours);
  var aColour = gl.getAttribLocation(program, 'aColour');
  gl.enableVertexAttribArray(aColour);

  var progressBuffer = makeBuffer(progress);
  var aProgress = gl.getAttribLocation(program, 'aProgress');
  gl.enableVertexAttribArray(aProgress);

  var uMVP = gl.getUniformLocation(program, 'uModelViewProjection');
  var uReveal = gl.getUniformLocation(program, 'uReveal');

  gl.enable(gl.DEPTH_TEST);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.clearColor(0.027, 0.031, 0.051, 1.0);

  var yaw = 0.4, pitch = -0.15, zoom = 2.6;
  var dragging = false, lastX = 0, lastY = 0, autoRotate = true;
  var vertexCount = segmentCount * 2;

  canvas.addEventListener('mousedown', function (e) {
    dragging = true; autoRotate = false; lastX = e.clientX; lastY = e.clientY;
  });
  window.addEventListener('mouseup', function () { dragging = false; });
  window.addEventListener('mousemove', function (e) {
    if (!dragging) return;
    yaw += (e.clientX - lastX) * 0.008;
    pitch += (e.clientY - lastY) * 0.008;
    pitch = Math.max(-1.5, Math.min(1.5, pitch));
    lastX = e.clientX; lastY = e.clientY;
  });
  canvas.addEventListener('wheel', function (e) {
    zoom *= Math.pow(1.0012, e.deltaY);
    zoom = Math.max(0.6, Math.min(12, zoom));
    e.preventDefault();
  }, { passive: false });
  canvas.addEventListener('touchstart', function (e) {
    if (e.touches.length === 1) {
      dragging = true; autoRotate = false;
      lastX = e.touches[0].clientX; lastY = e.touches[0].clientY;
    }
  }, { passive: true });
  window.addEventListener('touchend', function () { dragging = false; });
  window.addEventListener('touchmove', function (e) {
    if (!dragging || e.touches.length !== 1) return;
    yaw += (e.touches[0].clientX - lastX) * 0.008;
    pitch += (e.touches[0].clientY - lastY) * 0.008;
    pitch = Math.max(-1.5, Math.min(1.5, pitch));
    lastX = e.touches[0].clientX; lastY = e.touches[0].clientY;
  }, { passive: true });

  function resize() {
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = canvas.clientWidth * dpr;
    canvas.height = canvas.clientHeight * dpr;
    gl.viewport(0, 0, canvas.width, canvas.height);
  }
  window.addEventListener('resize', resize);
  resize();

  var start = performance.now();
  var REVEAL_MS = 2200;

  function frame(now) {
    var elapsed = now - start;
    if (autoRotate) yaw += 0.0011;

    var reveal = Math.min(1, elapsed / REVEAL_MS);
    reveal = reveal < 1 ? (1 - Math.pow(1 - reveal, 3)) : 1; // ease-out cubic

    var aspect = canvas.width / Math.max(1, canvas.height);
    var projection = mat4Perspective(0.6, aspect, 0.05, 50);
    var view = mat4Multiply(mat4Translate(0, 0, -zoom), mat4RotateX(pitch));
    view = mat4Multiply(view, mat4RotateY(yaw));
    var mvp = mat4Multiply(projection, view);

    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.useProgram(program);
    gl.uniformMatrix4fv(uMVP, false, mvp);
    gl.uniform1f(uReveal, reveal);

    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.vertexAttribPointer(aPosition, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, colourBuffer);
    gl.vertexAttribPointer(aColour, 3, gl.FLOAT, false, 0, 0);
    gl.bindBuffer(gl.ARRAY_BUFFER, progressBuffer);
    gl.vertexAttribPointer(aProgress, 1, gl.FLOAT, false, 0, 0);

    gl.drawArrays(gl.LINES, 0, vertexCount);
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
})();
</script>
</body>
</html>
"""


def write_streamline_viewer_html(streamlines, out_path, *, title="Fibre Field",
                                 subtitle="whole-brain DTI tractography",
                                 n_directions=None, seed_fa_threshold=0.25,
                                 max_total_points=1_200_000, seed=0):
    """Write a self-contained WebGL page rendering `streamlines` (world-mm
    (N,3) arrays, e.g. from generate_whole_brain_streamlines), coloured by
    local direction and grown in on load. No external JS/CSS beyond Google
    Fonts -- see the artifact CSP this is designed to also work as a
    published Artifact under.

    Substitutes into the template with plain string replacement on unique
    __TOKEN__ sentinels rather than str.format(): the template is mostly
    JavaScript, whose { and } are everywhere, and format() would treat every
    one of them as a field. html.escape() on the two free-text fields keeps
    a title/subtitle containing '<' or '&' from breaking out of the HTML
    text nodes they land in.

    max_total_points bounds the embedded (quantised int16, base64) payload
    -- an Artifact page is capped at 16MB total, and a subject with
    unusually long, low-curvature streamlines could otherwise blow past
    that. Whole streamlines are dropped at random to fit rather than
    truncating individual ones, which would leave visibly severed fibres.
    """
    if not streamlines:
        raise ValueError("No streamlines to render.")

    total_points = sum(len(line) for line in streamlines)
    if total_points > max_total_points:
        rng = np.random.default_rng(seed)
        order = rng.permutation(len(streamlines))
        kept, running = [], 0
        for i in order:
            running += len(streamlines[i])
            if running > max_total_points and kept:
                break
            kept.append(streamlines[i])
        streamlines = kept

    points, lengths, centre, scale = _quantise(streamlines)
    substitutions = {
        "__TITLE__": html_module.escape(title),
        "__SUBTITLE__": html_module.escape(subtitle),
        "__N_STREAMLINES__": str(len(streamlines)),
        "__N_DIRECTIONS__": str(n_directions) if n_directions is not None else "n/a",
        "__SEED_FA__": f"{seed_fa_threshold:.2f}",
        "__POINTS_B64__": _base64(points),
        "__LENGTHS_B64__": _base64(lengths),
        "__CENTRE_X__": repr(float(centre[0])),
        "__CENTRE_Y__": repr(float(centre[1])),
        "__CENTRE_Z__": repr(float(centre[2])),
        "__SCALE__": repr(float(scale)),
    }
    rendered = _VIEWER_TEMPLATE
    for token, value in substitutions.items():
        rendered = rendered.replace(token, value)

    with open(out_path, 'w') as handle:
        handle.write(rendered)
