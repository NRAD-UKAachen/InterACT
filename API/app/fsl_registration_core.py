"""FSL registration primitives, run locally in this container.

These used to live only in interact-gui (app/utils/fsl_clinical.py) and were
driven from here over HTTP, because FSL was installed there and not here.
That has not been true since FSL was bind-mounted into this container for
hippocampus_functions.run_first_all (see API/docker-compose.yml) -- the
comment claiming otherwise outlived the change. Running them here removes a
round-trip through the web tier, the FSL_CLINICAL_SERVICE_TOKEN that
authenticated it, and the HTTP fetch of every product back out of
interact-gui's static folder.

Same subprocess convention as hippocampus_functions.py: explicit FSLDIR /
FSLOUTPUTTYPE in the child environment. FSL_BIN is also prepended to PATH
because several of these are shell scripts (bet, slicer, pngappend,
fslreorient2std) that invoke other FSL binaries by bare name.
"""

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from .backend_helpers import log
from .job_logging import run_logged

FSLDIR = os.environ.get("FSLDIR", "/usr/local/fsl")
FSL_BIN = os.path.join(FSLDIR, "bin")
FSL_DATA = os.path.join(FSLDIR, "data")

MNI152_2MM = os.path.join(FSL_DATA, "standard", "MNI152_T1_2mm.nii.gz")


def _run_fsl(cmd, description: str = "", timeout: int = 7200) -> Dict[str, Any]:
    """Run one FSL command and report success rather than raising.

    Callers here chain several steps and decide per step whether a failure is
    fatal (a failed FLIRT is) or tolerable (a failed reorient falls back to
    the original image), so the result is returned as data.
    """
    env = {
        **os.environ,
        "FSLDIR": FSLDIR,
        "FSLOUTPUTTYPE": "NIFTI_GZ",
        "PATH": f"{FSL_BIN}:{os.environ.get('PATH', '')}",
    }
    log(f"[FSL] {description}: {' '.join(cmd)}")
    try:
        result = run_logged(cmd, env=env, timeout=timeout)
        success = result.returncode == 0
        if not success:
            log(f"[FSL] Failed: {result.stderr}")
        return {
            "success": success,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "command": " ".join(cmd),
            "description": description,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Timeout after {timeout}s",
                "command": " ".join(cmd), "description": description}
    except Exception as e:
        return {"success": False, "error": str(e),
                "command": " ".join(cmd), "description": description}


def reorient_to_std(input_image: str, output_image: str) -> Dict[str, Any]:
    """Losslessly permute/flip input_image onto FSL's standard axis order
    (fslreorient2std) -- a header/array rewrite, not a resample.

    FSL's own documentation recommends this before any FLIRT/FNIRT call.
    Some valid-but-non-axis-aligned acquisitions -- notably sagittally
    acquired T1s (e.g. "t1_mprage_sag"), whose NIfTI ends up with
    x=Anterior-Posterior and y=Right-Left instead of the standard
    x=Left-Right, y=Posterior-Anterior -- start FLIRT so far from its
    expected initial alignment that it can converge to a visibly distorted
    local optimum instead of failing outright, which is otherwise very hard
    to distinguish from a genuine registration failure.
    """
    Path(output_image).parent.mkdir(parents=True, exist_ok=True)
    cmd = [os.path.join(FSL_BIN, "fslreorient2std"), input_image, output_image]
    return _run_fsl(cmd, f"Reorient {Path(input_image).name} to standard orientation")


def bet_brain_extract(input_image: str, output_image: str, frac: float = 0.5,
                      robust: bool = True, mask: bool = True) -> Dict[str, Any]:
    """Brain extraction using FSL BET.

    frac is the fractional intensity threshold (0-1); smaller keeps more.
    """
    Path(output_image).parent.mkdir(parents=True, exist_ok=True)
    cmd = [os.path.join(FSL_BIN, "bet"), input_image, output_image, "-f", str(frac)]
    if robust:
        cmd.append("-R")
    if mask:
        cmd.append("-m")
    result = _run_fsl(cmd, f"BET brain extraction (f={frac})")
    if result["success"]:
        result["output_image"] = output_image
        mask_path = output_image.replace(".nii.gz", "_mask.nii.gz").replace(".nii", "_mask.nii")
        if os.path.exists(mask_path):
            result["mask_image"] = mask_path
    return result


def registration_quality_score(registered: str, reference: str) -> Dict[str, Any]:
    """Agreement between a registered volume and the template, via fslcc."""
    cmd = [os.path.join(FSL_BIN, "fslcc"), "--noabs", registered, reference]
    result = _run_fsl(cmd, "Registration quality score (fslcc)")
    score = None
    if result["success"]:
        lines = result["stdout"].strip().split("\n")
        if lines:
            parts = lines[0].split()
            if len(parts) >= 3:
                try:
                    score = float(parts[2])
                except ValueError:
                    pass
    return {"correlation": score, "raw_output": result.get("stdout", "")}


# The 12 slices the QC overlay is built from: four sagittal, four coronal and
# four axial, at the same fractional positions the interactive page has always
# used, appended into one PNG (4 wide, 3 tall).
_QC_SLICES = [("x", "sag"), ("y", "cor"), ("z", "axi")]
_QC_FRACTIONS = ["0.35", "0.45", "0.55", "0.65"]


def flirt_register_with_qc(input_image: str, reference: str, output_image: str,
                           output_dir: str, dof: int = 12,
                           cost: str = "corratio") -> Dict[str, Any]:
    """FLIRT registration plus a QC overlay PNG (slicer + pngappend).

    A failure to render the overlay is not treated as a registration
    failure: the registered volume is still valid and still returned, only
    without 'qc_image'.
    """
    Path(output_image).parent.mkdir(parents=True, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)

    omat = os.path.join(output_dir, "transform.mat")
    reg_result = _run_fsl([
        os.path.join(FSL_BIN, "flirt"),
        "-in", input_image, "-ref", reference, "-out", output_image,
        "-omat", omat, "-dof", str(dof), "-cost", cost,
    ], f"FLIRT registration (dof={dof})")

    if not reg_result["success"]:
        return reg_result

    reg_result["output_image"] = output_image
    reg_result["transform_matrix"] = omat

    slicer_cmd = [os.path.join(FSL_BIN, "slicer"), output_image, reference, "-s", "2"]
    tiles = []
    for axis, prefix in _QC_SLICES:
        for n, frac in enumerate(_QC_FRACTIONS, start=1):
            tile = os.path.join(output_dir, f"qc_{prefix}{n}.png")
            slicer_cmd += [f"-{axis}", frac, tile]
            tiles.append(tile)

    if _run_fsl(slicer_cmd, "Generate QC slices")["success"]:
        qc_image = os.path.join(output_dir, "qc_overlay.png")
        # pngappend joins with '+' across a row and '-' between rows.
        append_cmd = [os.path.join(FSL_BIN, "pngappend")]
        for row_start in range(0, len(tiles), len(_QC_FRACTIONS)):
            row = tiles[row_start:row_start + len(_QC_FRACTIONS)]
            if row_start:
                append_cmd.append("-")
            for n, tile in enumerate(row):
                if n:
                    append_cmd.append("+")
                append_cmd.append(tile)
        append_cmd.append(qc_image)
        if _run_fsl(append_cmd, "Combine QC slices")["success"]:
            reg_result["qc_image"] = qc_image

    reg_result["quality_score"] = registration_quality_score(output_image, reference)
    return reg_result


def fnirt_nonlinear_register(input_image: str, reference: str, output_image: str,
                             output_dir: str,
                             affine_matrix: Optional[str] = None) -> Dict[str, Any]:
    """Non-linear registration with FNIRT, on top of an affine start.

    Runs FLIRT itself first when no affine matrix is supplied.
    """
    os.makedirs(output_dir, exist_ok=True)
    Path(output_image).parent.mkdir(parents=True, exist_ok=True)

    if not affine_matrix:
        affine_out = os.path.join(output_dir, "affine_to_mni.nii.gz")
        affine_matrix = os.path.join(output_dir, "affine_to_mni.mat")
        flirt_result = _run_fsl([
            os.path.join(FSL_BIN, "flirt"),
            "-in", input_image, "-ref", reference,
            "-out", affine_out, "-omat", affine_matrix, "-dof", "12",
        ], "FLIRT affine pre-registration for FNIRT")
        if not flirt_result["success"]:
            return flirt_result

    cout = os.path.join(output_dir, "fnirt_warp_coeff.nii.gz")
    result = _run_fsl([
        os.path.join(FSL_BIN, "fnirt"),
        f"--in={input_image}", f"--ref={reference}",
        f"--aff={affine_matrix}", f"--cout={cout}", f"--iout={output_image}",
    ], "FNIRT non-linear registration", timeout=7200)
    if result["success"]:
        result["output_image"] = output_image
        result["warp_coeff"] = cout
    return result


def run_registration_qc(image: str, work_dir: str, dof: int = 12,
                        cost: str = "corratio", bet: bool = False,
                        bet_frac: float = 0.5, nonlinear: bool = False,
                        reference: Optional[str] = None) -> Dict[str, Any]:
    """Reorient -> optional BET -> FLIRT (+QC overlay) -> optional FNIRT.

    The same pipeline, in the same order, that interact-gui's
    /fsl-clinical/api/registration/qc runs, and it returns the same shape so
    callers that used to read that endpoint's JSON are unchanged:
    {'status', 'steps', 'qc_image', 'quality_score', 'registered_image'}.

    'registered_image' is always FLIRT's output, even for a non-linear run;
    FNIRT's warped volume is reached through the matching entry in 'steps'.
    The QC overlay and the correlation therefore describe the *affine* stage
    in both cases -- flirt_register_with_qc renders and scores before FNIRT
    runs on top of its matrix. Keeping both visible is what stops an affine
    result being handed back labelled non-linear.

    A custom `reference` disables BET and FNIRT: both of the latter are
    tuned for MNI152 (FNIRT's default T1_2_MNI152_2mm config in particular),
    and neither is meaningful against an arbitrary template.
    """
    # Checked up front rather than left to FSL: a missing input otherwise
    # only surfaces after fslreorient2std fails, falls back to the original
    # path, and FLIRT then spends ~30s on it before dying too.
    if not os.path.exists(image):
        raise FileNotFoundError(f"Input image not found: {image}")
    if reference and not os.path.exists(reference):
        raise FileNotFoundError(f"Reference template not found: {reference}")

    os.makedirs(work_dir, exist_ok=True)

    if reference:
        use_bet = False
        use_fnirt = False
    else:
        reference = MNI152_2MM
        use_bet = bet
        use_fnirt = nonlinear

    meta: Dict[str, Any] = {
        "status": "running", "input_image": image, "reference": reference,
    }
    steps = []

    # Every downstream step operates on the same reoriented copy, so the
    # affine matrix FNIRT reuses from the FLIRT step stays consistent with
    # what it is warping. Falls back to the original image rather than
    # aborting the whole registration if reorientation itself fails.
    std_image = os.path.join(work_dir, "input_std.nii.gz")
    reorient_result = reorient_to_std(image, std_image)
    steps.append(reorient_result)
    current = std_image if reorient_result.get("success") else image

    if use_bet:
        bet_out = os.path.join(work_dir, "brain.nii.gz")
        bet_result = bet_brain_extract(current, bet_out, frac=bet_frac)
        steps.append(bet_result)
        if bet_result["success"]:
            current = bet_out

    reg_out = os.path.join(work_dir, "registered.nii.gz")
    reg_result = flirt_register_with_qc(
        current, reference, reg_out, work_dir, dof=dof, cost=cost
    )
    steps.append(reg_result)

    if use_fnirt and reg_result["success"]:
        fnirt_out = os.path.join(work_dir, "registered_fnirt.nii.gz")
        steps.append(fnirt_nonlinear_register(
            current, reference, fnirt_out, work_dir,
            affine_matrix=reg_result.get("transform_matrix"),
        ))

    meta["status"] = "completed" if reg_result.get("success") else "failed"
    meta["steps"] = steps
    meta["qc_image"] = reg_result.get("qc_image")
    meta["quality_score"] = reg_result.get("quality_score")
    meta["registered_image"] = reg_out
    return meta
