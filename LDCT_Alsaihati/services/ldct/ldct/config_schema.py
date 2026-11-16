from dataclasses import dataclass
from pathlib import Path

@dataclass
class Config:
    output_dir: Path
    dicom_folder: Path
    f: float
    beta: float
    use_gpu: bool = False
    gpu_index: int = 0
