#!/usr/bin/env python3
"""Inference with a federated SegResNet (Auto3DSeg) model trained through
InterACT's fl-segresnet federation.

Two files always travel together and are both required: the trained
model (--checkpoint) and that same run's "recipe" (--recipe). The recipe
holds the settings the model was trained with -- image resolution,
intensity normalization, crop size, number of output classes -- and the
code that knows how to apply them. Without it, there is no way to
correctly prepare a new scan for this model. Both files sit next to each
other in the run's own folder and never change after round 0, so any
round's model can be paired with the same recipe file.

Checkpoint format: this project's own {"model": state_dict} convention
-- NOT Auto3DSeg's native {"state_dict": ...} checkpoint format. This
script converts automatically; you don't need to care about this
distinction.

Usage:
    python fl_segresnet_infer.py \
        --recipe /path/to/recipe.tar.gz \
        --checkpoint /path/to/final_model.pt \
        --input /path/to/case.nii.gz \
        --output-dir ./predictions

    # a whole folder of *.nii.gz volumes, and a recipe already extracted
    # (a directory works too, not just a .tar.gz)
    python fl_segresnet_infer.py \
        --recipe /path/to/recipe \
        --checkpoint /path/to/round_8/global_model_next.pt \
        --input /path/to/folder --output-dir ./predictions

To use a different model later (a later round, or a different run
entirely), just point --checkpoint at that file -- everything else about
this script stays the same, as long as --recipe is that SAME run's own
recipe file.

Output: <output-dir>/<case>_seg.nii.gz, an integer label map (0..C-1),
resampled back to the input volume's own original geometry -- the
network's internal resolution never leaks into the saved file. Label
meaning (what class 1, 2, ... stand for) is not recorded in the
checkpoint or recipe -- confirm with whoever ran the training job before
treating the numbers as meaningful.
"""
import argparse
import glob
import os
import shutil
import sys
import tarfile
import tempfile

import nibabel as nib
import numpy as np
import torch


def _prepare_bundle(recipe_path, checkpoint_path, work_dir):
    """Lays out <work_dir>/{scripts,configs,model/model.pt} -- the exact
    folder shape Auto3DSeg's own InferClass expects (bundle_root) --
    from whatever shape --recipe/--checkpoint were given in."""
    if os.path.isdir(recipe_path):
        recipe_root = recipe_path
        if os.path.isdir(os.path.join(recipe_root, "segresnet_0")):
            recipe_root = os.path.join(recipe_root, "segresnet_0")
    else:
        extract_dir = os.path.join(work_dir, "recipe_extracted")
        os.makedirs(extract_dir, exist_ok=True)
        with tarfile.open(recipe_path) as tar:
            tar.extractall(extract_dir)
        recipe_root = os.path.join(extract_dir, "segresnet_0")
        if not os.path.isdir(recipe_root):
            raise FileNotFoundError(
                f"expected {extract_dir}/segresnet_0 after extracting {recipe_path} -- "
                f"got: {os.listdir(extract_dir)}"
            )

    bundle = os.path.join(work_dir, "bundle")
    os.makedirs(os.path.join(bundle, "model"), exist_ok=True)
    shutil.copytree(os.path.join(recipe_root, "scripts"), os.path.join(bundle, "scripts"))
    shutil.copytree(os.path.join(recipe_root, "configs"), os.path.join(bundle, "configs"))

    obj = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    if "model" in obj:
        state_dict = obj["model"]  # this project's FL convention
    elif "state_dict" in obj:
        state_dict = obj["state_dict"]  # already Auto3DSeg's own format
    else:
        raise ValueError(
            f"{checkpoint_path}: expected a 'model' or 'state_dict' key, got {list(obj.keys())}"
        )
    torch.save({"state_dict": state_dict}, os.path.join(bundle, "model", "model.pt"))

    return bundle


def _list_inputs(input_path):
    if os.path.isdir(input_path):
        files = sorted(glob.glob(os.path.join(input_path, "*.nii.gz")))
        if not files:
            raise FileNotFoundError(f"no *.nii.gz files found under {input_path}")
        return files
    return [input_path]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recipe", required=True, help="recipe.tar.gz, or an already-extracted recipe directory")
    ap.add_argument("--checkpoint", required=True, help="final_model.pt or any round_<N>/global_model_next.pt")
    ap.add_argument("--input", required=True, help="a single *.nii.gz file, or a folder of them")
    ap.add_argument("--output-dir", required=True)
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    inputs = _list_inputs(args.input)

    with tempfile.TemporaryDirectory() as work_dir:
        bundle = _prepare_bundle(args.recipe, args.checkpoint, work_dir)
        sys.path.insert(0, os.path.join(bundle, "scripts"))
        from infer import InferClass  # noqa: E402  -- part of the recipe bundle, not vendored here

        infer = InferClass(config_file=os.path.join(bundle, "configs", "hyper_parameters.yaml"), bundle_root=bundle)

        for image_path in inputs:
            case_id = os.path.basename(image_path).replace(".nii.gz", "")
            pred = infer.infer({"image": image_path})
            pred_labels = torch.argmax(pred, dim=0).cpu().numpy().astype(np.uint8)

            img_nii = nib.load(image_path)
            out_path = os.path.join(args.output_dir, f"{case_id}_seg.nii.gz")
            nib.save(nib.Nifti1Image(pred_labels, img_nii.affine, img_nii.header), out_path)
            print(f"{case_id}: wrote {out_path}  shape={pred_labels.shape}  classes_present={sorted(set(pred_labels.flatten().tolist()))}")


if __name__ == "__main__":
    main()
