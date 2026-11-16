"""
Main OHIF API Application
Unified API gateway for multiple medical imaging processing services
"""
from flask import Flask, jsonify
from flask_cors import CORS
import sys
from pathlib import Path

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

# Import configuration
from config import settings
from utils.job_manager import JobManager

# Import service blueprints
from services.ldct.ldct.blueprint import ldct_bp


def create_app():
    """Application factory"""
    app = Flask(__name__)
    CORS(app, origins=settings.CORS_ORIGINS)

    # Load configuration
    app.config['DEBUG'] = settings.DEBUG
    app.config['JOB_MANAGER'] = JobManager()

    # Register service blueprints
    # Note: Nginx strips /api/ prefix, so /api/ldct/simulate -> /ldct/simulate
    app.register_blueprint(ldct_bp, url_prefix='/ldct')

    # Future services can be registered here:
    # app.register_blueprint(segmentation_bp, url_prefix='/api/segmentation')
    # app.register_blueprint(registration_bp, url_prefix='/api/registration')

    # Global routes
    @app.route('/')
    def index():
        """API information endpoint"""
        return jsonify({
            "name": "OHIF Medical Imaging API",
            "version": "1.0.0",
            "services": [
                {
                    "name": "LDCT Simulation",
                    "endpoint": "/api/ldct",
                    "description": "Low-Dose CT simulation from full-dose DICOM",
                    "ui": "/api/ldct/"
                },
                # Future services will be listed here
            ]
        })

    @app.route('/api/health')
    def health_check():
        """Health check endpoint"""
        job_manager = app.config['JOB_MANAGER']
        return jsonify({
            "status": "healthy",
            "services": ["ldct"],
            "active_jobs": len([j for j in job_manager.list_jobs() if j['status'] == 'running']),
            "total_jobs": len(job_manager.list_jobs())
        })

    @app.route('/api/jobs')
    def list_all_jobs():
        """List all jobs across all services"""
        job_manager = app.config['JOB_MANAGER']
        return jsonify({
            "jobs": job_manager.list_jobs()
        })

    @app.route('/api/jobs/<job_id>')
    def get_job(job_id):
        """Get job status by ID (works across all services)"""
        job_manager = app.config['JOB_MANAGER']
        job = job_manager.get_job(job_id)
        if not job:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(job)

    return app


# Create the application instance
app = create_app()


if __name__ == "__main__":
    print("=" * 60)
    print("🏥 OHIF Medical Imaging API Starting...")
    print("=" * 60)
    print(f"Host: {settings.HOST}")
    print(f"Port: {settings.PORT}")
    print(f"Debug: {settings.DEBUG}")
    print("\nAvailable Services:")
    print("  - LDCT Simulation: http://{}:{}/api/ldct/".format(settings.HOST, settings.PORT))
    print("\nAPI Documentation: http://{}:{}/".format(settings.HOST, settings.PORT))
    print("Health Check: http://{}:{}/api/health".format(settings.HOST, settings.PORT))
    print("=" * 60)

    app.run(
        host=settings.HOST,
        port=settings.PORT,
        debug=settings.DEBUG
    )
