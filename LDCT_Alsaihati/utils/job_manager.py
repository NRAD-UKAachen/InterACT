"""Shared job management utilities"""
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
import uuid


class JobManager:
    """Manages jobs across different services"""

    def __init__(self):
        self.jobs: Dict[str, Dict[str, Any]] = {}

    def create_job(self, service: str, request_data: dict, **kwargs) -> str:
        """
        Create a new job.

        Args:
            service: Name of the service (e.g., 'ldct', 'segmentation')
            request_data: Original request data
            **kwargs: Additional job metadata

        Returns:
            job_id: Unique job identifier
        """
        job_id = str(uuid.uuid4())
        self.jobs[job_id] = {
            "job_id": job_id,
            "service": service,
            "status": "pending",
            "progress": 0.0,
            "message": "Job queued",
            "created_at": datetime.now().isoformat(),
            "started_at": None,
            "completed_at": None,
            "error": None,
            "request": request_data,
            **kwargs
        }
        return job_id

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job information by ID"""
        return self.jobs.get(job_id)

    def update_job(self, job_id: str, **updates):
        """Update job information"""
        if job_id in self.jobs:
            self.jobs[job_id].update(updates)

    def delete_job(self, job_id: str) -> bool:
        """Delete a job"""
        if job_id in self.jobs:
            del self.jobs[job_id]
            return True
        return False

    def list_jobs(self, service: Optional[str] = None) -> list:
        """
        List all jobs, optionally filtered by service.

        Args:
            service: Optional service name to filter by

        Returns:
            List of job dictionaries
        """
        if service:
            return [job for job in self.jobs.values() if job.get("service") == service]
        return list(self.jobs.values())

    def cleanup_old_jobs(self, hours: int = 24):
        """Remove jobs older than specified hours"""
        cutoff = datetime.now() - timedelta(hours=hours)
        jobs_to_delete = []

        for job_id, job in self.jobs.items():
            created_at = datetime.fromisoformat(job["created_at"])
            if created_at < cutoff and job["status"] in ["completed", "failed"]:
                jobs_to_delete.append(job_id)

        for job_id in jobs_to_delete:
            del self.jobs[job_id]

        return len(jobs_to_delete)
