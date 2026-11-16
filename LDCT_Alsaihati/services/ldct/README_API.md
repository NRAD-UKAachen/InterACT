# LDCT Simulation Tool - API Version

This is the API version of the LDCT simulation tool, providing a web-based interface to replace YAML configuration files.

## Features

- **Web UI**: Beautiful popup interface for configuring simulations
- **Real-time Progress**: Live progress tracking during the 12-step process
- **RESTful API**: Programmatic access to LDCT simulations
- **Background Processing**: Non-blocking job execution
- **Job Management**: Track multiple concurrent simulations

## Quick Start

### 1. Build the Docker Image

```bash
docker build -t ldct_api .
```

### 2. Run the API Server

```bash
docker run --rm --gpus all \
  -p 8000:8000 \
  -v /path/to/your/data:/mnt/data \
  ldct_api
```

### 3. Access the Web UI

Open your browser and navigate to:
```
http://localhost:8000
```

You'll see a popup form where you can:
- Set output directory
- Select DICOM folder
- Configure dose parameters (f, beta)
- Choose GPU settings
- Select start/stop steps

## API Endpoints

### GET `/`
Returns the web UI interface

### POST `/api/simulate`
Start a new LDCT simulation job

**Request Body:**
```json
{
  "output_dir": "/mnt/data/output",
  "dicom_folder": "/mnt/data/dicom",
  "ldct_numpy_filename": "ldct_sim.npy",
  "ldct_nrrd_filename": "ldct_sim.nrrd",
  "f": 0.25,
  "beta": 0.5,
  "use_gpu": true,
  "gpu_index": 0,
  "start_at": "step0",
  "stop_after": null
}
```

**Response:**
```json
{
  "job_id": "123e4567-e89b-12d3-a456-426614174000",
  "message": "Job started"
}
```

### GET `/api/status/{job_id}`
Get the status of a running job

**Response:**
```json
{
  "job_id": "123e4567-e89b-12d3-a456-426614174000",
  "status": "running",
  "current_step": "step3",
  "progress": 25.0,
  "message": "Running step3: Forward projection",
  "started_at": "2026-01-14T10:30:00",
  "completed_at": null,
  "error": null
}
```

**Status values:**
- `pending` - Job queued, not started
- `running` - Job in progress
- `completed` - Job finished successfully
- `failed` - Job failed with error

### GET `/api/jobs`
List all jobs

**Response:**
```json
{
  "jobs": [
    {
      "job_id": "...",
      "status": "completed",
      ...
    }
  ]
}
```

### DELETE `/api/jobs/{job_id}`
Delete a job from history

## Configuration Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `output_dir` | string | required | Path where results will be saved |
| `dicom_folder` | string | required | Path to original DICOM series |
| `ldct_numpy_filename` | string | `ldct_sim.npy` | Output NumPy filename |
| `ldct_nrrd_filename` | string | `ldct_sim.nrrd` | Output NRRD filename |
| `f` | float | `0.25` | Dose scaling factor (0.25 = 25% dose) |
| `beta` | float | `0.5` | Dose-noise power relationship |
| `use_gpu` | boolean | `true` | Enable GPU acceleration |
| `gpu_index` | integer | `0` | GPU device index |
| `start_at` | string | `step0` | Start from this step |
| `stop_after` | string | `null` | Stop after this step (optional) |

## Pipeline Steps

The simulation runs through 12 steps:

1. **step0** - DICOM to NumPy conversion
2. **step1** - GNI preprocessing
3. **step2** - NPS computation
4. **step3** - Forward projection
5. **step4** - Add noise
6. **step5** - Back projection
7. **step6_1** - NPS noise image generation
8. **step6_2** - Filter noise
9. **step7_1** - Noise GNI
10. **step7_2** - Scale noise
11. **step8_1** - Save LDCT as NumPy
12. **step8_2** - Save LDCT as NRRD

## Example Usage

### Using the Web UI
1. Navigate to `http://localhost:8000`
2. Fill in the form with your paths and parameters
3. Click "Start Simulation"
4. Watch real-time progress
5. Get notified when complete

### Using cURL
```bash
curl -X POST http://localhost:8000/api/simulate \
  -H "Content-Type: application/json" \
  -d '{
    "output_dir": "/mnt/data/output",
    "dicom_folder": "/mnt/data/dicom",
    "f": 0.25,
    "beta": 0.5,
    "use_gpu": true
  }'
```

### Using Python
```python
import requests
import time

# Start simulation
response = requests.post('http://localhost:8000/api/simulate', json={
    'output_dir': '/mnt/data/output',
    'dicom_folder': '/mnt/data/dicom',
    'f': 0.25,
    'beta': 0.5,
    'use_gpu': True
})

job_id = response.json()['job_id']

# Poll for status
while True:
    status = requests.get(f'http://localhost:8000/api/status/{job_id}').json()
    print(f"Progress: {status['progress']:.1f}% - {status['message']}")

    if status['status'] in ['completed', 'failed']:
        break

    time.sleep(2)
```

## Running Without Docker (CLI mode)

If you have the conda environment set up locally, you can still use the CLI:

```bash
conda activate astra_env
python -m ldct.run --config your_config.yaml
```

## Volume Mounts

Make sure to mount your data directories:

```bash
docker run --rm --gpus all \
  -p 8000:8000 \
  -v /path/to/dicom:/mnt/dicom \
  -v /path/to/output:/mnt/output \
  ldct_api
```

Then in the web UI or API calls, use `/mnt/dicom` and `/mnt/output` as paths.

## Troubleshooting

### GPU not available
- Ensure NVIDIA Container Toolkit is installed
- Use `--gpus all` flag when running docker
- Check GPU availability with `nvidia-smi`

### Path not found errors
- Verify volume mounts in docker run command
- Use absolute paths in the API requests
- Ensure paths exist and have proper permissions

### Port already in use
Change the port mapping:
```bash
docker run --rm --gpus all -p 9000:8000 ... ldct_api
```

Then access at `http://localhost:9000`

## Development

To run the API in development mode with auto-reload:

```bash
conda activate astra_env
export FLASK_ENV=development
python -m ldct.api
```

Or modify the Dockerfile entrypoint:
```dockerfile
ENTRYPOINT ["micromamba", "run", "-n", "astra_env", "flask", "run", "--host=0.0.0.0", "--port=8000"]
```

## Original CLI Documentation

For the original YAML-based CLI documentation, see [README.md](README.md)
