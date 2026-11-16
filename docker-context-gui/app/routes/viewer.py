#app/routes/viewer.py

from flask import Blueprint, render_template
from app.utils.roles import admin_required

viewer_bp = Blueprint('viewer', __name__, template_folder='templates')


@viewer_bp.route('/ohif')
def ohif():
    return render_template('ohif.html', title='OHIF Viewer')


@viewer_bp.route('/database')
@admin_required
def database():
    # Renders templates/database.html, which extends base.html & embeds the
    # DCM4CHEE iframe via the /dcm4chee-arc/ proxy (see dcm4chee_proxy.py).
    return render_template('database.html', title='Database')
