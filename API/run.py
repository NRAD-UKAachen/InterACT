from app import create_app

app = create_app()

if __name__ == "__main__":
    # threaded=True: without it, Flask's dev server handles one request at a
    # time, so a long synchronous handler (POST /batch/run's series->role
    # matching + dcm2niix conversion over a whole cohort, which can run for
    # well over an hour on a large one - see docker-context-gui's submit())
    # blocks every other request on this server, including GET
    # /batch/status/<job_id> polls for jobs that are already running. Safe to
    # add: batch_jobs.py already guards its shared `_jobs` dict with its own
    # lock for exactly this kind of concurrent access from background worker
    # threads, same pattern as docker-context-gui's own threaded dev server
    # (Dockerfile's `flask run --with-threads`).
    app.run(debug=False, host="0.0.0.0", port=9200, threaded=True)