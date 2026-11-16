#!/usr/bin/env python3
"""One federated round for the SegResNet/Auto3DSeg FL model. Two distinct
round types, chosen by the ROUND_TYPE env var (set by
dag_fl_segresnet_local.py):

  fingerprint  Round 0 only. Analyzes ONLY this site's own local data
               (per-case numbers -- spacing, intensity, shape -- never
               raw images) and writes that list out. Does NOT train
               anything.

  train        Every round after 0. Loads the shared recipe the
               coordinator generated from every site's merged
               fingerprint (RECIPE_DIR, already downloaded+extracted by
               the local DAG before this container starts), points it
               at THIS site's own local data, trains for LOCAL_EPOCHS,
               and writes out the resulting weights in this codebase's
               own {"model": state_dict} convention -- NOT
               Auto3DSeg's own {"state_dict": ...} checkpoint format --
               so the coordinator's plain-FedAvg aggregation code needs
               no per-model special-casing.

Pure local compute only in both modes -- no network calls; the local
DAG does all fetching and uploading around this container.
"""
import glob
import json
import os
import sys

import torch


def env(name, default=None, cast=str):
    val = os.environ.get(name, default)
    return cast(val) if val is not None else None


def _build_datalist(local_data_dir):
    # federated_training_data.py's set_batch_enabled lets an admin leave
    # a batch out of training without deleting it -- enabled_cases.json
    # (written by that same route, into this same folder -- no separate
    # mount needed) names exactly which case IDs currently qualify. Its
    # absence means every case on disk trains, same as before this
    # feature existed -- so an older site with no Training Data batches
    # UI usage yet, or a hand-populated folder, is unaffected.
    enabled_path = os.path.join(local_data_dir, "enabled_cases.json")
    enabled_cases = None
    if os.path.isfile(enabled_path):
        with open(enabled_path) as f:
            enabled_cases = set(json.load(f)["enabled_cases"])
        print(f"enabled_cases.json present: {len(enabled_cases)} case(s) enabled for training")

    images = sorted(glob.glob(os.path.join(local_data_dir, "images", "*.nii.gz")))
    entries = []
    skipped_disabled = 0
    for image_path in images:
        case_id = os.path.basename(image_path)
        if enabled_cases is not None and case_id not in enabled_cases:
            skipped_disabled += 1
            continue
        label_path = os.path.join(local_data_dir, "labels", case_id)
        if os.path.isfile(label_path):
            entries.append({"image": image_path, "label": label_path})
    if skipped_disabled:
        print(f"skipped {skipped_disabled} case(s) belonging to a disabled batch")
    if not entries:
        raise FileNotFoundError(
            f"no matched, enabled image/label pairs found under {local_data_dir} "
            f"(expected images/<case>.nii.gz + labels/<case>.nii.gz, and not "
            f"left out by a disabled batch on the Training Data page)"
        )
    return entries


def run_fingerprint(local_data_dir, output_path):
    """This site's half of the fingerprint round: per-case stats only,
    via the same DataAnalyzer machinery Auto3DSeg itself uses -- just
    scoped to this site's own datalist, and stopped before the
    "combine everything" half (that only happens once, centrally, after
    every site's list is collected -- see merge step in the coordinator
    DAG)."""
    import yaml
    from monai.apps.auto3dseg.data_analyzer import DataAnalyzer

    entries = _build_datalist(local_data_dir)
    print(f"fingerprint round: {len(entries)} local cases")

    analyzer = DataAnalyzer(
        datalist={"training": entries},
        dataroot="",
        output_path="/tmp/_unused_datastats.yaml",
        device="cuda" if torch.cuda.is_available() else "cpu",
    )
    result = analyzer.get_all_case_stats()
    by_case = result["stats_by_cases"]

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        yaml.safe_dump(
            {"stats_by_cases": by_case, "n_cases": len(by_case)},
            f, default_flow_style=None, sort_keys=False,
        )
    print(f"wrote {len(by_case)}-case fingerprint to {output_path}")


INITIAL_WEIGHTS_SEED = 1234  # must be the same at every site


def _shm_safe_cache_rate(config_file, n_cases):
    """Auto3DSeg sizes its in-RAM dataset cache from free host RAM, but
    stores that cache in shared memory (/dev/shm), which inside this
    container is capped far lower -- on large volumes the cache outgrows
    it and DataLoader workers die with "Bus error". Cap the cache at what
    /dev/shm and free RAM can really hold. Below 10% Auto3DSeg itself
    turns caching off, so do the same."""
    import shutil
    import numpy as np
    import yaml

    with open(config_file) as f:
        cfg = yaml.safe_load(f)
    if cfg.get("resample") and cfg.get("image_size_mm_90"):
        size = (np.array(cfg["image_size_mm_90"]) / np.array(cfg["resample_resolution"])).astype(np.int64)
    else:
        size = np.array(cfg["image_size"])
    required = (4 * cfg["input_channels"] + 1) * int(np.prod(size)) * n_cases
    with open("/proc/meminfo") as f:
        avail = next(int(line.split()[1]) * 1024 for line in f if line.startswith("MemAvailable"))
    budget = 0.6 * min(shutil.disk_usage("/dev/shm").total, avail)
    rate = min(1.0, budget / required)
    return 0.0 if rate < 0.1 else rate


def run_training(local_data_dir, recipe_dir, global_model_path, local_epochs, output_path):
    entries = _build_datalist(local_data_dir)
    print(f"train round: {len(entries)} local cases, {local_epochs} local epoch(s)")

    round_ckpt_dir = os.path.join(os.path.dirname(output_path), "segresnet_ckpt")
    os.makedirs(round_ckpt_dir, exist_ok=True)
    local_datalist_path = os.path.join(round_ckpt_dir, "local_datalist.json")
    with open(local_datalist_path, "w") as f:
        json.dump({"training": entries, "testing": []}, f)

    config_file = os.path.join(recipe_dir, "configs", "hyper_parameters.yaml")
    scripts_dir = os.path.join(recipe_dir, "scripts")
    sys.path.insert(0, scripts_dir)
    from segmenter import run_segmenter  # noqa: E402  -- part of the downloaded recipe bundle, not vendored here

    overrides = {
        "data_file_base_dir": "",
        "data_list_file_path": local_datalist_path,
        "num_epochs": local_epochs,
        "auto_scale_allowed": False,  # a stronger/weaker local GPU must not silently pick a different patch size
        "validate_final_original_res": False,  # no per-site validation split -- real eval happens externally
        "ckpt_path": round_ckpt_dir,
    }
    if global_model_path and global_model_path != "NONE":
        # The incoming file is in this codebase's own {"model": state_dict}
        # convention (what _aggregate_round writes), not Auto3DSeg's own
        # checkpoint format ({"state_dict": ...}) that its finetune loader
        # expects -- convert before handing it off.
        incoming = torch.load(global_model_path, map_location="cpu", weights_only=False)
        finetune_ckpt_path = os.path.join(round_ckpt_dir, "finetune_source.pt")
        torch.save({"state_dict": incoming["model"]}, finetune_ckpt_path)
        overrides["finetune#enabled"] = True
        overrides["finetune#ckpt_name"] = finetune_ckpt_path
        # checkpoint_load() unconditionally does start_epoch += 1 after
        # loading ANY checkpoint, every time, with no way to opt out --
        # it assumes it's resuming one continuous run (epoch N complete,
        # so resume at N+1). Every FL round is a fresh process/container,
        # so that +1 always lands on 1 regardless of round number, and
        # combined with num_epochs=local_epochs=1 that makes
        # range(1, 1) empty -- training silently runs zero epochs and
        # exits "successfully" with no checkpoint written. Setting -1
        # here cancels out that automatic +1, landing back on 0 so
        # range(0, local_epochs) actually runs.
        overrides["start_epoch"] = -1
    else:
        # First training round: no global model exists yet, so each site
        # would draw its own random starting weights, and averaging those
        # unrelated networks cancels most of what round 1 learned. Every
        # site seeds identically so they all start from the same weights.
        torch.manual_seed(INITIAL_WEIGHTS_SEED)
        torch.cuda.manual_seed_all(INITIAL_WEIGHTS_SEED)
    overrides["cache_rate"] = _shm_safe_cache_rate(config_file, len(entries))
    print(f"training overrides: {overrides}", flush=True)

    best_metric = run_segmenter(config_file=config_file, **overrides)
    print(f"run_segmenter returned, best_metric={best_metric}", flush=True)
    print(f"contents of {round_ckpt_dir}: {os.listdir(round_ckpt_dir)}", flush=True)

    trained_ckpt = os.path.join(round_ckpt_dir, "model_final.pt")
    checkpoint = torch.load(trained_ckpt, map_location="cpu", weights_only=False)
    state_dict = checkpoint["state_dict"]  # Auto3DSeg's own checkpoint_save format

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    torch.save({"model": state_dict, "n_cases": len(entries)}, output_path)
    print(f"wrote local update ({len(entries)} cases) to {output_path}")


def run_merge(round_0_upload_dir, site_names, modality, recipe_dir, recipe_tarball_path):
    """Merges every site's uploaded fingerprint into one combined list,
    then generates the shared recipe -- invoked by
    the coordinator DAG as a `docker run` of this image instead of
    running in Airflow's own process, since that process doesn't have
    monai installed."""
    import json as json_mod
    import shutil
    import tarfile

    import yaml
    from monai.apps.auto3dseg.bundle_gen import BundleGen
    from monai.auto3dseg import SegSummarizer

    combined = []
    for name in site_names:
        # federated_rounds.upload_round_result always nests a site's
        # upload under uploaded/, regardless of round type.
        path = os.path.join(round_0_upload_dir, name, "uploaded", "fingerprint.yaml")
        with open(path) as f:
            d = yaml.safe_load(f)
        combined.extend(d["stats_by_cases"])
        print(f"fingerprint merge: {name} contributed {d['n_cases']} cases")
    print(f"fingerprint merge: {len(combined)} cases total")

    summarizer = SegSummarizer(image_key="image", label_key="label", do_ccp=False, hist_bins=0)
    summary = summarizer.summarize(combined)
    summary["n_cases"] = len(combined)
    merged_datastats_path = os.path.join(recipe_dir, "merged_datastats.yaml")
    os.makedirs(recipe_dir, exist_ok=True)
    with open(merged_datastats_path, "w") as f:
        yaml.safe_dump({"stats_summary": summary, "stats_by_cases": combined}, f, default_flow_style=None, sort_keys=False)

    empty_datalist_path = os.path.join(recipe_dir, "empty_datalist.json")
    with open(empty_datalist_path, "w") as f:
        json_mod.dump({"training": [], "testing": []}, f)
    data_src_cfg_path = os.path.join(recipe_dir, "data_src_cfg.json")
    with open(data_src_cfg_path, "w") as f:
        # dataroot/datalist are placeholders -- every site overrides
        # these with its own real local paths when it actually trains.
        json_mod.dump({"modality": modality, "dataroot": "UNUSED", "datalist": empty_datalist_path}, f)

    bundle_gen = BundleGen(
        algo_path=recipe_dir,
        algos="segresnet",
        data_stats_filename=merged_datastats_path,
        data_src_cfg_name=data_src_cfg_path,
    )
    bundle_gen.generate(recipe_dir, num_fold=1)
    print(f"generated shared recipe at {recipe_dir}/segresnet_0")

    with tarfile.open(recipe_tarball_path, "w:gz") as tar:
        tar.add(os.path.join(recipe_dir, "segresnet_0"), arcname="segresnet_0")
    print(f"packaged recipe as {recipe_tarball_path}")

    # Ship the matching inference script alongside the recipe, once, here
    # -- neither ever changes again for the rest of this run, so there's
    # no reason to regenerate either per round. Baked into this image at
    # build time (files/fl_segresnet_infer.py); copied out to the run's
    # own folder so an admin can download both from the GUI once the run
    # finishes (federated_rounds.download_deliverable) without ever
    # touching a backend shell.
    script_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fl_segresnet_infer.py")
    script_dst = os.path.join(os.path.dirname(recipe_tarball_path), "inference_script.py")
    shutil.copy(script_src, script_dst)
    print(f"copied inference script to {script_dst}")

    # Ships alongside the script above so someone running it doesn't
    # have to guess what to pip install, or discover it's missing one
    # error at a time -- this is NOT this image's own build-time
    # requirements.txt (that one's for building THIS container; someone
    # running the standalone inference script never needs to build this
    # image at all). Pinned to the same versions either way, so
    # inference genuinely reproduces the environment the model was
    # trained under.
    reqs_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inference_requirements.txt")
    reqs_dst = os.path.join(os.path.dirname(recipe_tarball_path), "requirements.txt")
    shutil.copy(reqs_src, reqs_dst)
    print(f"copied inference requirements to {reqs_dst}")


def main():
    round_type = env("ROUND_TYPE", "train")

    if round_type == "fingerprint":
        run_fingerprint(env("LOCAL_DATA_DIR"), os.path.join(env("OUTPUT_DIR"), "fingerprint.yaml"))
    elif round_type == "merge":
        run_merge(
            round_0_upload_dir=env("ROUND_0_UPLOAD_DIR"),
            site_names=env("SITE_NAMES").split(","),
            modality=env("MODALITY", "CT"),
            recipe_dir=env("RECIPE_DIR"),
            recipe_tarball_path=env("RECIPE_TARBALL_PATH"),
        )
    else:
        run_training(
            local_data_dir=env("LOCAL_DATA_DIR"),
            recipe_dir=env("RECIPE_DIR"),
            global_model_path=env("GLOBAL_MODEL_PATH", "NONE"),
            local_epochs=env("LOCAL_EPOCHS", "1", int),
            output_path=os.path.join(env("OUTPUT_DIR"), "local_update.pt"),
        )


if __name__ == "__main__":
    main()
