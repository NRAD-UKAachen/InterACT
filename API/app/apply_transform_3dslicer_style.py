"""
Apply an ANTs/ITK-style .mat transform to a segmentation NIfTI, resampling it
onto a reference image's grid -- the same operation 3D Slicer's "Resample
Scalar/Vector/DWI Volume" module performs. Used by
brats_functions._transform_brats_segmentation() to warp a BraTS segmentation
from atlas space back into the patient's native space.
"""

import os
import numpy as np
import nibabel as nib
import SimpleITK as sitk
from scipy.io import loadmat


class TransformApplier:
    """Loads a .mat transform and applies it to a segmentation via SimpleITK."""

    def __init__(self, verbose=True):
        self.verbose = verbose

    def log(self, message):
        """Print `message` if verbose mode is enabled."""
        if self.verbose:
            print(f"[INFO] {message}")

    def load_transform_mat(self, transform_file):
        """Load a transform from a MATLAB .mat file. Supports the three
        formats ANTs/ITK tooling commonly writes: the ITK
        'AffineTransform_float_3_3' + 'fixed' pair, a flat 12-element array,
        or a plain 4x4 matrix."""
        try:
            mat = loadmat(transform_file)

            if 'AffineTransform_float_3_3' in mat:
                params = [float(p) for p in mat['AffineTransform_float_3_3'].flatten()]
                fixed = [float(f) for f in mat['fixed'].flatten()] if 'fixed' in mat else [0, 0, 0]

                affine = sitk.AffineTransform(3)
                affine.SetMatrix(params[0:9])
                affine.SetTranslation(params[9:12])
                affine.SetCenter(fixed[0:3])
                return affine

            for k, v in mat.items():
                if k.startswith('__') or not isinstance(v, np.ndarray) or not np.issubdtype(v.dtype, np.number):
                    continue
                if v.size == 12:
                    # MATLAB column-major -> reshape
                    affine_3x4 = v.flatten().reshape((3, 4), order='F')
                    return np.vstack([affine_3x4, [0, 0, 0, 1]])
                if v.shape == (4, 4):
                    return v

            raise ValueError("Unsupported transform format in .mat file")

        except Exception as e:
            raise ValueError(f"Error loading transform file: {str(e)}")

    def apply_transform_sitk(self, segmentation_file, transform, reference_file, output_file):
        """Resample `segmentation_file` onto `reference_file`'s grid using
        the inverse of `transform` (atlas space -> original space) and
        nearest-neighbor interpolation, so label values are preserved."""
        self.log("Loading images...")
        segmentation = sitk.ReadImage(segmentation_file)
        reference = sitk.ReadImage(reference_file)

        self.log(f"Segmentation size: {segmentation.GetSize()}")
        self.log(f"Reference size: {reference.GetSize()}")

        if isinstance(transform, np.ndarray) and transform.shape == (4, 4):
            affine = sitk.AffineTransform(3)
            affine.SetMatrix(transform[:3, :3].flatten())
            affine.SetTranslation(transform[:3, 3])
            transform = affine

        inverse_transform = (
            transform.GetInverse() if hasattr(transform, 'GetInverse') else np.linalg.inv(transform)
        )

        self.log("Applying transform...")
        resampled_segmentation = sitk.Resample(
            segmentation,
            reference,
            inverse_transform,
            sitk.sitkNearestNeighbor,  # nearest neighbor for labels
            0.0  # default value for background
        )

        self.log(f"Saving result to {output_file}")
        sitk.WriteImage(resampled_segmentation, output_file)

        return resampled_segmentation

    def validate_files(self, segmentation_file, transform_file, reference_file):
        """Raise if any of the three input files is missing or unreadable."""
        for file_path, file_type in [(segmentation_file, "segmentation"),
                                    (transform_file, "transform"),
                                    (reference_file, "reference")]:
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"{file_type.capitalize()} file not found: {file_path}")

            if not os.access(file_path, os.R_OK):
                raise PermissionError(f"Cannot read {file_type} file: {file_path}")

    def apply_transform(self, segmentation_file, transform_file, reference_file, output_file, validate=True):
        """Load `transform_file` (.mat) and apply it to `segmentation_file`,
        writing the result to `output_file`. Set validate=False to skip the
        upfront file-existence/readability check."""
        try:
            if validate:
                self.validate_files(segmentation_file, transform_file, reference_file)

            self.log(f"Loading transform from {transform_file}")
            transform = self.load_transform_mat(transform_file)

            return self.apply_transform_sitk(segmentation_file, transform, reference_file, output_file)

        except Exception as e:
            self.log(f"Error: {str(e)}")
            raise
