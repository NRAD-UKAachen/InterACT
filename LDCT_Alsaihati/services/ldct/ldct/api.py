from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from pathlib import Path
from typing import Optional, Dict, Any
import threading
import json
from datetime import datetime
import uuid
import SimpleITK as sitk

from .config_schema import Config
from .steps import (
    step0_dcm2npy, step1_gni, step2_FG, step3_fp, step4_addnoise, step5_bp,
    step6_1_NPSnoiseimage, step6_2_filternoise, step7_1_noisegni, step7_2_scalenoise,
    step8_1_ldct_numpy, step8_2_saveasnifti, step8_3_saveasdicom, step8_4_upload_dicomweb
)

from config.base_storage import STORAGE_PATHS

app = Flask(__name__)
CORS(app)

# Global state for tracking jobs
jobs: Dict[str, Dict[str, Any]] = {}

STEPS = [
    ("step0", "DICOM to NumPy conversion", step0_dcm2npy.run),
    ("step1", "GNI preprocessing", step1_gni.run),
    ("step2", "NPS computation", step2_FG.run),
    ("step3", "Forward projection", step3_fp.run),
    ("step4", "Add noise", step4_addnoise.run),
    ("step5", "Back projection", step5_bp.run),
    ("step6_1", "NPS noise image generation", step6_1_NPSnoiseimage.run),
    ("step6_2", "Filter noise", step6_2_filternoise.run),
    ("step7_1", "Noise GNI", step7_1_noisegni.run),
    ("step7_2", "Scale noise", step7_2_scalenoise.run),
    ("step8_1", "Save LDCT as NumPy", step8_1_ldct_numpy.run),
    ("step8_2", "Save LDCT as NIfTI", step8_2_saveasnifti.run),
    ("step8_3", "Save LDCT as DICOM series", step8_3_saveasdicom.run),
    ("step8_4", "Upload to DICOMweb server", step8_4_upload_dicomweb.run),
]

# Base storage directory for all outputs
BASE_STORAGE_DIR = STORAGE_PATHS["ldct_alsaihati"]

def extract_patient_name(dicom_folder: Path) -> str:
    """Extract series description from DICOM files for folder naming"""
    try:
        # Get DICOM series file names
        series_file_names = sitk.ImageSeriesReader.GetGDCMSeriesFileNames(str(dicom_folder))
        if not series_file_names:
            raise FileNotFoundError(f"No DICOM files found in {dicom_folder}")

        # Read the first DICOM file to get series description
        reader = sitk.ImageFileReader()
        reader.SetFileName(series_file_names[0])
        reader.LoadPrivateTagsOn()
        reader.ReadImageInformation()

        # Try to get series description (DICOM tag 0008,103E)
        series_description = reader.GetMetaData("0008|103e") if reader.HasMetaDataKey("0008|103e") else ""

        # Clean up series description for use in file path
        if series_description:
            # Replace special characters and spaces
            series_description = series_description.replace("^", "_").replace(" ", "_").replace("/", "_")
            series_description = "".join(c for c in series_description if c.isalnum() or c == "_")

        # If no series description found, use a default
        if not series_description:
            series_description = f"unknown_series_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        return series_description
    except Exception as e:
        # Fallback to timestamp-based name if extraction fails
        print(f"Warning: Could not extract series description: {e}")
        return f"unknown_series_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def build_config(request_data: dict) -> Config:
    """Build Config object from API request"""
    dicom_folder = Path(request_data['dicom_folder'])

    # Use user-provided series description for folder naming, fallback to DICOM extraction
    user_series_desc = request_data.get('output_series_description', '').strip()
    if user_series_desc:
        # Clean up user input for use in file path
        series_description = user_series_desc.replace("^", "_").replace(" ", "_").replace("/", "_").replace("-", "_")
        series_description = "".join(c for c in series_description if c.isalnum() or c == "_")
    else:
        # Fallback: Extract series description from DICOM files
        series_description = extract_patient_name(dicom_folder)

    # Create output directory based on series description
    output_dir = BASE_STORAGE_DIR / series_description

    # Ensure the base directory exists
    BASE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

    config = Config(
        output_dir=output_dir,
        dicom_folder=dicom_folder,
        f=float(request_data['f']),
        beta=float(request_data['beta']),
        use_gpu=bool(request_data.get('use_gpu', True)),
        gpu_index=int(request_data.get('gpu_index', 0)),
    )

    # Attach filenames
    setattr(config, 'ldct_numpy_filename', request_data.get('ldct_numpy_filename', 'ldct_sim.npy'))
    setattr(config, 'ldct_nifti_filename', request_data.get('ldct_nifti_filename', 'ldct_sim.nii.gz'))
    setattr(config, 'series_description', series_description)  # Store series description in config for reference
    setattr(config, 'output_series_description', request_data.get('output_series_description', 'LDCT Simulation'))
    setattr(config, 'upload_to_dicomweb', request_data.get('upload_to_dicomweb', True))

    return config


def run_ldct_pipeline(job_id: str, request_data: dict):
    """Execute LDCT pipeline with progress tracking"""
    jobs[job_id]["status"] = "running"
    jobs[job_id]["started_at"] = datetime.now().isoformat()

    try:
        cfg = build_config(request_data)

        # Find start and stop indices
        start_at = request_data.get('start_at', 'step0')
        stop_after = request_data.get('stop_after')

        step_ids = [s[0] for s in STEPS]
        start_idx = step_ids.index(start_at) if start_at in step_ids else 0
        stop_idx = step_ids.index(stop_after) + 1 if stop_after and stop_after in step_ids else len(STEPS)

        total_steps = stop_idx - start_idx

        for i, (step_id, description, func) in enumerate(STEPS[start_idx:stop_idx], start=1):
            jobs[job_id]["current_step"] = step_id
            jobs[job_id]["message"] = f"Running {step_id}: {description}"
            jobs[job_id]["progress"] = (i - 1) / total_steps * 100

            # Run the step
            func(cfg)

            jobs[job_id]["progress"] = i / total_steps * 100

        jobs[job_id]["status"] = "completed"
        jobs[job_id]["message"] = "LDCT simulation completed successfully"
        jobs[job_id]["completed_at"] = datetime.now().isoformat()

    except Exception as e:
        jobs[job_id]["status"] = "failed"
        jobs[job_id]["error"] = str(e)
        jobs[job_id]["message"] = f"Failed: {str(e)}"
        jobs[job_id]["completed_at"] = datetime.now().isoformat()


HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>LDCT Simulation Tool</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 600px;
            width: 100%;
            padding: 40px;
        }
        h1 {
            color: #333;
            margin-bottom: 10px;
            font-size: 28px;
        }
        .subtitle {
            color: #666;
            margin-bottom: 30px;
            font-size: 14px;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            color: #333;
            font-weight: 500;
            font-size: 14px;
        }
        .label-desc {
            font-weight: normal;
            color: #666;
            font-size: 12px;
        }
        input, select {
            width: 100%;
            padding: 12px;
            border: 2px solid #e1e8ed;
            border-radius: 6px;
            font-size: 14px;
            transition: border-color 0.3s;
        }
        input:focus, select:focus {
            outline: none;
            border-color: #667eea;
        }
        .row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }
        .checkbox-group {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .checkbox-group input[type="checkbox"] {
            width: auto;
        }
        button {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        button:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.4);
        }
        button:active {
            transform: translateY(0);
        }
        button:disabled {
            background: #ccc;
            cursor: not-allowed;
            transform: none;
        }
        .progress-container {
            display: none;
            margin-top: 20px;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 6px;
        }
        .progress-bar {
            width: 100%;
            height: 8px;
            background: #e1e8ed;
            border-radius: 4px;
            overflow: hidden;
            margin-bottom: 15px;
        }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #667eea 0%, #764ba2 100%);
            width: 0%;
            transition: width 0.3s;
        }
        .progress-text {
            font-size: 14px;
            color: #333;
            margin-bottom: 8px;
        }
        .progress-step {
            font-size: 12px;
            color: #666;
        }
        .error {
            background: #fee;
            color: #c33;
            padding: 12px;
            border-radius: 6px;
            margin-top: 15px;
            font-size: 14px;
        }
        .success {
            background: #efe;
            color: #3a3;
            padding: 12px;
            border-radius: 6px;
            margin-top: 15px;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🏥 LDCT Simulation Tool</h1>
        <p class="subtitle">Low-Dose CT Image Simulation from Full-Dose DICOM Series</p>

        <form id="ldctForm">
            <div class="form-group">
                <label>DICOM Folder <span class="label-desc">(original full-dose DICOM series)</span></label>
                <input type="text" id="dicom_folder" required placeholder="/path/to/dicom">
            </div>

            <div class="form-group">
                <label>Output Series Description <span class="label-desc">(for folder name & DICOM output)</span></label>
                <input type="text" id="output_series_description" value="LDCT Simulation" required placeholder="LDCT Simulation">
            </div>

            <div class="form-group">
                <label>Output Directory <span class="label-desc">(auto-generated: ldct_alsaihati/[series description])</span></label>
                <input type="text" id="output_dir" readonly style="background-color: #f5f5f5; cursor: not-allowed;" placeholder="Auto-generated from above">
            </div>

            <div class="row">
                <div class="form-group">
                    <label>NumPy Filename</label>
                    <input type="text" id="ldct_numpy_filename" value="ldct_sim.npy">
                </div>
                <div class="form-group">
                    <label>NIfTI Filename</label>
                    <input type="text" id="ldct_nifti_filename" value="ldct_sim.nii.gz">
                </div>
            </div>

            <div class="row">
                <div class="form-group">
                    <label>Dose Factor (f) <span class="label-desc">0.25 = 25% dose</span></label>
                    <input type="number" id="f" value="0.25" min="0.01" max="1" step="0.01" required>
                </div>
                <div class="form-group">
                    <label>Beta (β) <span class="label-desc">dose-noise relationship</span></label>
                    <input type="number" id="beta" value="0.5" min="0" max="1" step="0.1" required>
                </div>
            </div>

            <div class="row">
                <div class="form-group">
                    <label>Start At Step</label>
                    <select id="start_at">
                        <option value="step0" selected>step0 - DICOM to NumPy</option>
                        <option value="step1">step1 - GNI preprocessing</option>
                        <option value="step2">step2 - NPS computation</option>
                        <option value="step3">step3 - Forward projection</option>
                        <option value="step4">step4 - Add noise</option>
                        <option value="step5">step5 - Back projection</option>
                        <option value="step6_1">step6_1 - NPS noise image</option>
                        <option value="step6_2">step6_2 - Filter noise</option>
                        <option value="step7_1">step7_1 - Noise GNI</option>
                        <option value="step7_2">step7_2 - Scale noise</option>
                        <option value="step8_1">step8_1 - Save NumPy</option>
                        <option value="step8_2">step8_2 - Save NIfTI</option>
                        <option value="step8_3">step8_3 - Save DICOM</option>
                        <option value="step8_4">step8_4 - Upload to DICOMweb</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Stop After Step <span class="label-desc">(optional)</span></label>
                    <select id="stop_after">
                        <option value="">Run all steps</option>
                        <option value="step0">step0</option>
                        <option value="step1">step1</option>
                        <option value="step2">step2</option>
                        <option value="step3">step3</option>
                        <option value="step4">step4</option>
                        <option value="step5">step5</option>
                        <option value="step6_1">step6_1</option>
                        <option value="step6_2">step6_2</option>
                        <option value="step7_1">step7_1</option>
                        <option value="step7_2">step7_2</option>
                        <option value="step8_1">step8_1</option>
                        <option value="step8_2">step8_2</option>
                        <option value="step8_3">step8_3</option>
                        <option value="step8_4">step8_4</option>
                    </select>
                </div>
            </div>

            <div class="row">
                <div class="form-group checkbox-group">
                    <input type="checkbox" id="use_gpu" checked>
                    <label for="use_gpu" style="margin-bottom: 0;">Use GPU Acceleration</label>
                </div>
                <div class="form-group">
                    <label>GPU Index</label>
                    <input type="number" id="gpu_index" value="0" min="0">
                </div>
            </div>

            <div class="form-group checkbox-group">
                <input type="checkbox" id="upload_to_dicomweb" checked>
                <label for="upload_to_dicomweb" style="margin-bottom: 0;">Upload DICOM to DICOMweb Server</label>
            </div>

            <button type="submit" id="submitBtn">Start Simulation</button>
        </form>

        <div class="progress-container" id="progressContainer">
            <div class="progress-text" id="progressText">Progress: 0%</div>
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill"></div>
            </div>
            <div class="progress-step" id="progressStep">Initializing...</div>
        </div>

        <div id="message"></div>
    </div>

    <script>
        const form = document.getElementById('ldctForm');
        const submitBtn = document.getElementById('submitBtn');
        const progressContainer = document.getElementById('progressContainer');
        const progressFill = document.getElementById('progressFill');
        const progressText = document.getElementById('progressText');
        const progressStep = document.getElementById('progressStep');
        const messageDiv = document.getElementById('message');

        form.addEventListener('submit', async (e) => {
            e.preventDefault();

            const formData = {
                dicom_folder: document.getElementById('dicom_folder').value,
                output_series_description: document.getElementById('output_series_description').value,
                ldct_numpy_filename: document.getElementById('ldct_numpy_filename').value,
                ldct_nifti_filename: document.getElementById('ldct_nifti_filename').value,
                f: parseFloat(document.getElementById('f').value),
                beta: parseFloat(document.getElementById('beta').value),
                use_gpu: document.getElementById('use_gpu').checked,
                gpu_index: parseInt(document.getElementById('gpu_index').value),
                upload_to_dicomweb: document.getElementById('upload_to_dicomweb').checked,
                start_at: document.getElementById('start_at').value,
                stop_after: document.getElementById('stop_after').value || null
            };

            submitBtn.disabled = true;
            submitBtn.textContent = 'Starting...';
            messageDiv.innerHTML = '';
            progressContainer.style.display = 'block';

            try {
                const response = await fetch('/api/simulate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(formData)
                });

                const data = await response.json();

                if (!response.ok) {
                    throw new Error(data.error || 'Failed to start simulation');
                }

                // Display the auto-generated output directory
                document.getElementById('output_dir').value = data.output_dir;
                messageDiv.innerHTML = `<div class="success">Series: ${data.series_description}<br>Output: ${data.output_dir}</div>`;

                const jobId = data.job_id;
                pollJobStatus(jobId);

            } catch (error) {
                messageDiv.innerHTML = `<div class="error">Error: ${error.message}</div>`;
                submitBtn.disabled = false;
                submitBtn.textContent = 'Start Simulation';
                progressContainer.style.display = 'none';
            }
        });

        async function pollJobStatus(jobId) {
            const interval = setInterval(async () => {
                try {
                    const response = await fetch(`/api/status/${jobId}`);
                    const status = await response.json();

                    progressFill.style.width = status.progress + '%';
                    progressText.textContent = `Progress: ${Math.round(status.progress)}%`;
                    progressStep.textContent = status.message;

                    if (status.status === 'completed') {
                        clearInterval(interval);
                        messageDiv.innerHTML = '<div class="success">✓ Simulation completed successfully!</div>';
                        submitBtn.disabled = false;
                        submitBtn.textContent = 'Start Simulation';
                    } else if (status.status === 'failed') {
                        clearInterval(interval);
                        messageDiv.innerHTML = `<div class="error">✗ ${status.error || 'Simulation failed'}</div>`;
                        submitBtn.disabled = false;
                        submitBtn.textContent = 'Start Simulation';
                    }
                } catch (error) {
                    clearInterval(interval);
                    messageDiv.innerHTML = `<div class="error">Error checking status: ${error.message}</div>`;
                    submitBtn.disabled = false;
                    submitBtn.textContent = 'Start Simulation';
                }
            }, 1000);
        }
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    """Serve the configuration UI"""
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/simulate', methods=['POST'])
def start_simulation():
    """Start a new LDCT simulation job"""
    request_data = request.get_json()

    if not request_data:
        return jsonify({"error": "No data provided"}), 400

    # Validate required fields (output_dir is no longer required, it's auto-generated)
    required_fields = ['dicom_folder', 'f', 'beta']
    for field in required_fields:
        if field not in request_data:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    # Validate DICOM folder exists
    dicom_folder = Path(request_data['dicom_folder'])
    if not dicom_folder.exists():
        return jsonify({"error": f"DICOM folder does not exist: {request_data['dicom_folder']}"}), 400

    # Extract series description and determine output directory
    try:
        # Use user-provided series description for folder naming, fallback to DICOM extraction
        user_series_desc = request_data.get('output_series_description', '').strip()
        if user_series_desc:
            # Clean up user input for use in file path
            series_description = user_series_desc.replace("^", "_").replace(" ", "_").replace("/", "_").replace("-", "_")
            series_description = "".join(c for c in series_description if c.isalnum() or c == "_")
        else:
            # Fallback: Extract series description from DICOM files
            series_description = extract_patient_name(dicom_folder)
        output_dir = BASE_STORAGE_DIR / series_description

        # Ensure the base directory exists
        BASE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"error": f"Failed to process DICOM folder: {str(e)}"}), 400

    # Create job
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "current_step": None,
        "progress": 0.0,
        "message": "Job queued",
        "started_at": None,
        "completed_at": None,
        "error": None,
        "request": request_data,
        "series_description": series_description,
        "output_dir": str(output_dir)
    }

    # Start background thread
    thread = threading.Thread(target=run_ldct_pipeline, args=(job_id, request_data))
    thread.daemon = True
    thread.start()

    return jsonify({
        "job_id": job_id,
        "message": "Job started",
        "series_description": series_description,
        "output_dir": str(output_dir)
    }), 200


@app.route('/api/status/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """Get the status of a running job"""
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    return jsonify(jobs[job_id]), 200


@app.route('/api/jobs', methods=['GET'])
def list_jobs():
    """List all jobs"""
    return jsonify({"jobs": list(jobs.values())}), 200


@app.route('/api/jobs/<job_id>', methods=['DELETE'])
def delete_job(job_id):
    """Delete a job from history"""
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    del jobs[job_id]
    return jsonify({"message": "Job deleted"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
