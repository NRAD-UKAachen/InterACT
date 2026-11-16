from __future__ import annotations
import json, yaml, typer, sys
from pathlib import Path
from typing import Optional
from .config_schema import Config
from .steps import (
    step0_dcm2npy, step1_gni, step2_FG, step3_fp, step4_addnoise, step5_bp,
    step6_1_NPSnoiseimage, step6_2_filternoise, step7_1_noisegni, step7_2_scalenoise,
    step8_1_ldct_numpy, step8_2_saveasnifti
)

app = typer.Typer(add_completion=False)

def load_config(config_path: Path) -> dict:
    data = yaml.safe_load(config_path.read_text()) if config_path.suffix in {".yml", ".yaml"} else json.loads(config_path.read_text())
    return data

# def build_config(base: dict, **overrides) -> Config:
#     # merge base + non-None overrides
#     cfg = {**base, **{k: v for k, v in overrides.items() if v is not None}}
#     return Config(
#         output_dir=Path(cfg["output_dir"]),
#         dicom_folder=Path(cfg["dicom_folder"]),
#         f=float(cfg["f"]),
#         beta=float(cfg["beta"]),
#         use_gpu=bool(cfg.get("use_gpu", True)),
#         gpu_index=int(cfg.get("gpu_index", 0)),
#     )
def build_config(base: dict, **overrides) -> Config:
    cfg_dict = {**base, **{k: v for k, v in overrides.items() if v is not None}}
    config = Config(
        output_dir=Path(cfg_dict["output_dir"]),
        dicom_folder=Path(cfg_dict["dicom_folder"]),
        f=float(cfg_dict["f"]),
        beta=float(cfg_dict["beta"]),
        use_gpu=bool(cfg_dict.get("use_gpu", True)),
        gpu_index=int(cfg_dict.get("gpu_index", 0)),
    )

    # Attach any other keys dynamically for optional step parameters
    for k, v in cfg_dict.items():
        if not hasattr(config, k):
            setattr(config, k, v)

    return config

@app.command()
def run(
    config: Path = typer.Option(Path("config.default.yaml"), help="YAML/JSON config."),
    output_dir: Optional[Path] = None,
    dicom_folder: Optional[Path] = None,
    f: Optional[float] = None,
    beta: Optional[float] = None,
    use_gpu: Optional[bool] = typer.Option(None, help="Override GPU usage from config."),
    gpu_index: Optional[int] = typer.Option(None, help="Override GPU index from config."),
    start_at: str = typer.Option("step0", help="Start from this step id."),
    stop_after: Optional[str] = typer.Option(None, help="Stop after this step id."),
    dry_run: bool = False,
):
    base = load_config(config)
    cfg = build_config(
        base,
        output_dir=output_dir,
        dicom_folder=dicom_folder,
        f=f,
        beta=beta,
        use_gpu=use_gpu,
        gpu_index=gpu_index,
    )

    steps = [
        ("step0", step0_dcm2npy.run),
        ("step1", step1_gni.run),
        ("step2", step2_FG.run),
        ("step3", step3_fp.run),
        ("step4", step4_addnoise.run),
        ("step5", step5_bp.run),
        ("step6_1", step6_1_NPSnoiseimage.run),
        ("step6_2", step6_2_filternoise.run),
        ("step7_1", step7_1_noisegni.run),
        ("step7_2", step7_2_scalenoise.run),
        ("step8_1", step8_1_ldct_numpy.run),
        ("step8_2", step8_2_saveasnifti.run),
    ]

    execute = False if start_at else True
    for sid, func in steps:
        if sid == start_at:
            execute = True
        if not execute:
            continue
        typer.echo(f"▶ {sid}")
        if dry_run:
            continue
        func(cfg)
        if stop_after and sid == stop_after:
            break

if __name__ == "__main__":
    app()
