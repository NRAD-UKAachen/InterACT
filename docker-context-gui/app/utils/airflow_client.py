"""
Airflow REST API client for triggering and monitoring DAG runs.

Uses the Airflow 2.x stable REST API (``/api/v1``).
Authentication is basic-auth by default.
"""

import os
import logging

import requests

logger = logging.getLogger(__name__)


class AirflowClient:
    """Thin wrapper around the Airflow REST API."""

    def __init__(self):
        self.base_url = os.environ.get(
            "AIRFLOW_API_URL", "http://airflow-webserver:8080/api/v1"
        )
        self.auth = (
            os.environ.get("AIRFLOW_API_USER", "admin"),
            os.environ.get("AIRFLOW_API_PASSWORD", "admin"),
        )
        self.timeout = 15

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------
    def health(self):
        """Return Airflow health status dict or ``None`` on failure."""
        try:
            resp = requests.get(
                f"{self.base_url}/health",
                auth=self.auth,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Airflow health check failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------
    def trigger_dag(self, dag_id, conf=None, dag_run_id=None):
        """Trigger a DAG run. Returns the created DAG-run JSON."""
        payload = {"conf": conf or {}}
        if dag_run_id:
            payload["dag_run_id"] = dag_run_id

        resp = requests.post(
            f"{self.base_url}/dags/{dag_id}/dagRuns",
            json=payload,
            auth=self.auth,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Status queries
    # ------------------------------------------------------------------
    def get_dag_run(self, dag_id, dag_run_id):
        """Get overall status of a single DAG run."""
        resp = requests.get(
            f"{self.base_url}/dags/{dag_id}/dagRuns/{dag_run_id}",
            auth=self.auth,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def get_task_instances(self, dag_id, dag_run_id):
        """Get per-task status for a DAG run."""
        resp = requests.get(
            f"{self.base_url}/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances",
            auth=self.auth,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------
    def list_dag_runs(self, dag_id, limit=25, order_by="-execution_date"):
        """List recent DAG runs for a given dag_id."""
        resp = requests.get(
            f"{self.base_url}/dags/{dag_id}/dagRuns",
            params={"limit": limit, "order_by": order_by},
            auth=self.auth,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()
