# Custom Airflow image with this project's Python dependencies baked in,
# replacing _PIP_ADDITIONAL_REQUIREMENTS (which Airflow's own docs mark as
# quick-start/dev only -- it re-runs pip install on every container start).
# Add a new DAG's dependency to requirements.txt, then rebuild this image
# once (`docker compose build`) instead of growing a docker-compose.yml
# env var line forever.
FROM apache/airflow:2.9.3-python3.11

COPY requirements.txt /requirements.txt
RUN pip install --no-cache-dir -r /requirements.txt
