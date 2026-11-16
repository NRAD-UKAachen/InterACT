from app import create_app

app = create_app()

if __name__ == "__main__":
    # threaded=True so a single slow/large request (e.g. a large NRRD/NIfTI
    # upload) doesn't tie up Werkzeug's dev server and starve/stall other
    # requests -- previously caused large uploads to silently truncate.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
