# -*- coding: utf-8 -*-
"""
Created on Mon Apr  7 17:00:56 2025

@author: jdeiters
"""
# app/routes/home.py
import fcntl
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, abort
from app.oidc_auth import oidc
from app.security import current_user_is_admin

home_bp = Blueprint('home', __name__)

# Bug reports live in the uids_logs directory (it has write permissions), one
# JSON object per line: {"id", "username", "created", "message"}.
REPORTS_FILE = '/app/uids_logs/bug_reports.jsonl'
# Pre-ownership format: one plain-text message per line, no author. Imported
# once into REPORTS_FILE (author unknown, so only admins see those) and renamed.
LEGACY_MESSAGES_FILE = '/app/uids_logs/messages.txt'
LOCK_FILE = REPORTS_FILE + '.lock'


@contextmanager
def _reports_lock():
    """Serialize read-modify-write cycles across gunicorn/flask workers."""
    with open(LOCK_FILE, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _write_reports(reports):
    tmp = REPORTS_FILE + '.tmp'
    with open(tmp, 'w') as file:
        for report in reports:
            file.write(json.dumps(report) + "\n")
    os.replace(tmp, REPORTS_FILE)


def _migrate_legacy_messages():
    if os.path.exists(REPORTS_FILE) or not os.path.exists(LEGACY_MESSAGES_FILE):
        return
    with open(LEGACY_MESSAGES_FILE, 'r') as file:
        reports = [
            {'id': uuid.uuid4().hex, 'username': None, 'created': None, 'message': line.strip()}
            for line in file if line.strip()
        ]
    _write_reports(reports)
    os.replace(LEGACY_MESSAGES_FILE, LEGACY_MESSAGES_FILE + '.migrated')


def _read_reports():
    _migrate_legacy_messages()
    try:
        with open(REPORTS_FILE, 'r') as file:
            return [json.loads(line) for line in file if line.strip()]
    except FileNotFoundError:
        return []


def read_reports():
    with _reports_lock():
        return _read_reports()


def add_report(username, message):
    with _reports_lock():
        _migrate_legacy_messages()
        with open(REPORTS_FILE, 'a') as file:
            file.write(json.dumps({
                'id': uuid.uuid4().hex,
                'username': username,
                'created': datetime.now().isoformat(timespec='seconds'),
                'message': message,
            }) + "\n")


def delete_report_by_id(report_id, username, is_admin):
    """Remove a report if it exists and the caller may delete it.
    Returns False when the report is missing, None when it's not the caller's."""
    with _reports_lock():
        reports = _read_reports()
        for i, report in enumerate(reports):
            if report.get('id') == report_id:
                if not (is_admin or (username and report.get('username') == username)):
                    return None
                reports.pop(i)
                _write_reports(reports)
                return True
        return False


def _current_username():
    info = oidc.user_getinfo(['preferred_username', 'email']) or {}
    return info.get('preferred_username')


@home_bp.route('/', methods=['GET', 'POST'])
@oidc.require_login
def home():
    username = _current_username()

    if request.method == 'POST':
        new_message = (request.form.get('message') or '').strip()
        if new_message and username:
            add_report(username, new_message)
        return redirect(url_for('home.home'))

    is_admin = current_user_is_admin(oidc)
    reports = read_reports()
    if not is_admin:
        reports = [r for r in reports if username and r.get('username') == username]
    # Newest first; legacy reports without a timestamp sink to the bottom.
    reports.sort(key=lambda r: r.get('created') or '', reverse=True)

    return render_template(
        'home.html',
        reports=reports,
        token=oidc.get_access_token(),
        user=username or 'Unknown',
        is_admin=is_admin
    )


@home_bp.route('/delete-report/<report_id>', methods=['POST'])
@oidc.require_login
def delete_report(report_id):
    """Delete a bug report: admins may delete any, users only their own."""
    result = delete_report_by_id(report_id, _current_username(), current_user_is_admin(oidc))
    if result is None:
        abort(403)
    return redirect(url_for('home.home'))
