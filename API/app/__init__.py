from flask import Flask

def create_app():
    """Flask application factory: registers the `main` blueprint (all routes,
    see routes.py) and adds the CORS headers every response needs."""
    app = Flask(__name__)

    # First, so every line from here on -- including the batch jobs resumed
    # below -- lands in API_storage/logs/ (see job_logging.py).
    from .job_logging import configure_logging
    configure_logging()

    from .routes import main
    app.register_blueprint(main)

    # Importing routes.py above runs its batch_jobs.register_runner(...)
    # calls, so every job kind has a runner by the time this loads persisted
    # job files and resumes any that were still pending/running when the
    # container last stopped (see batch_jobs.load_jobs_from_disk).
    from . import batch_jobs
    batch_jobs.load_jobs_from_disk()

    # After the load above, so the sweep's batch pass sees the registry that
    # tells a live job's work dir from one a deleted job left behind.
    from . import storage_cleanup
    storage_cleanup.start_retention_thread()

    # Download brainles_preprocessing's atlases/SynthStrip weights in the
    # background if this host doesn't have them yet, so a fresh install has
    # them before the first coreg/BraTS job -- see brainles_assets.py.
    from . import brainles_assets
    brainles_assets.start_prefetch_thread()

    @app.after_request
    def add_cors_headers(response):
        # Every existing route only ever received plain GETs (a CORS "simple
        # request"), so per-route Access-Control-Allow-Origin headers (see
        # jobs.cors_json/cors_file) were enough. A POST with a JSON body
        # (e.g. /coreg) isn't "simple", so the browser sends an OPTIONS
        # preflight first -- Flask answers that automatically, bypassing our
        # route handlers entirely, so it needs CORS headers added here.
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        response.headers.setdefault("Access-Control-Allow-Headers", "Content-Type")
        return response

    return app