# -*- coding: utf-8 -*-
"""
SAMinRAD - Medical Image Segmentation Application
Main application built with Flask.

This application enables segmentation of medical images
(DICOM, NIfTI, NRRD) using SAM2 and threshold methods.
"""

import os
import logging
from flask import Flask

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def create_app():
    """
    Factory function for creating the Flask app.

    Returns:
        Flask: Configured Flask application
    """
    # Create Flask app
    app = Flask(__name__)

    # Secret key for sessions
    app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'radsam-secret-key-change-in-production')

    # Load configuration
    from config import Config
    app.config['MAX_CONTENT_LENGTH'] = 2 * 1024 * 1024 * 1024  # 2 GB max upload

    # Create directories
    Config.create_directories()

    # Register blueprints
    from routes import register_blueprints
    register_blueprints(app)

    # Initialize SAM2
    logger.info("Initializing SAM2...")
    from services.sam_service import initialize_sam2
    initialize_sam2()

    logger.info("RADSAM application initialized successfully.")

    return app


# Create Flask app
app = create_app()


if __name__ == '__main__':
    from config import Config

    # Show configuration
    Config.print_config()

    # Start server
    logger.info(f"Starting RADSAM on {Config.FLASK_CONFIG['host']}:{Config.FLASK_CONFIG['port']}")
    app.run(
        host=Config.FLASK_CONFIG['host'],
        port=Config.FLASK_CONFIG['port'],
        debug=Config.FLASK_CONFIG['debug']
    )
