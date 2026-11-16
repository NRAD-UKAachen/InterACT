# -*- coding: utf-8 -*-
"""
Routes module for RADSAM.
Contains all Flask blueprints for the various functional areas.
"""

from flask import Blueprint

# Blueprint definitions
main_bp = Blueprint('main', __name__)
upload_bp = Blueprint('upload', __name__)
image_bp = Blueprint('image', __name__)
segmentation_bp = Blueprint('segmentation', __name__)
threshold_bp = Blueprint('threshold', __name__)
fewshot_bp = Blueprint('fewshot', __name__)
download_bp = Blueprint('download', __name__)
comparison_bp = Blueprint('comparison', __name__)

# Import route handlers after blueprint definition (to avoid circular imports)
from . import main_routes
from . import upload_routes
from . import image_routes
from . import segmentation_routes
from . import threshold_routes
from . import fewshot_routes
from . import download_routes
from . import comparison_routes


def register_blueprints(app):
    """
    Registers all blueprints with the Flask app.

    Args:
        app: Flask application instance
    """
    app.register_blueprint(main_bp)
    app.register_blueprint(upload_bp)
    app.register_blueprint(image_bp)
    app.register_blueprint(segmentation_bp)
    app.register_blueprint(threshold_bp)
    app.register_blueprint(fewshot_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(comparison_bp)
