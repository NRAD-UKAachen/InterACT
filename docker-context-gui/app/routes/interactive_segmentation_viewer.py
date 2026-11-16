# app/routes/interactive_segmentation_viewer.py
from flask import Blueprint, render_template
import time


interactive_bp = Blueprint("interactive", __name__)  # blueprint name = "interactive"

@interactive_bp.route("/colleague_iframe")
def colleague_iframe():
    # cache-buster to avoid stale iframe
    return render_template("interactive_iframe.html", cb=int(time.time()))
