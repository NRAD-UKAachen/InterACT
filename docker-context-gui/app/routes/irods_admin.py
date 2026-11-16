# app/routes/irods_admin.py
"""
Admin-only iRODS user, group and permission management.

Provisioning is deliberately simple: an iRODS user IS a Keycloak user - we
create a same-named account with no password, because nobody ever authenticates
to iRODS directly. People authenticate to Keycloak, and the iRODS HTTP API maps
their token's "preferred_username" claim onto this account.

Every route is gated by @admin_required (Keycloak "Admin" group), but that only
decides who sees the page. The operations themselves run against the iRODS HTTP
API with the admin's *own* token, so they additionally require that person to be
a rodsadmin in iRODS - promote them with:

    iadmin moduser <username> type rodsadmin

That double gate is intentional: being an admin of the web GUI no longer implies
holding the zone's master credential, which is what the old proxy design meant.
"""
import re

from flask import Blueprint, render_template, request, redirect, url_for, flash

from app.utils.roles import admin_required
from app.utils import irods_client as irods
from app.utils.irods_client import IrodsError, IrodsPermissionDenied

irods_admin_bp = Blueprint("irods_admin", __name__, template_folder="templates")

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# Membership of these is managed by iRODS itself; editing them here does more
# harm than good.
PROTECTED_GROUPS = {"public", "rodsadmin"}


def _embedded():
    return request.values.get("embedded") or None


def _back():
    return redirect(url_for("irods_admin.manage_users", embedded=_embedded()))


def _needs_rodsadmin(exc):
    """Turn iRODS' bare permission refusal into something actionable."""
    return (
        f"{exc} Your Keycloak account is an Admin here, but the matching iRODS "
        "account is not a rodsadmin - run: iadmin moduser "
        f"{irods.current_username()} type rodsadmin"
    )


@irods_admin_bp.route("/irods-admin/users")
@admin_required
def manage_users():
    users, groups, memberships = [], [], {}
    try:
        users = irods.list_users()
        groups = irods.list_groups()
        memberships = {group: irods.group_members(group) for group in groups}
    except IrodsError as exc:
        flash(str(exc), "error")

    return render_template(
        "irods_admin_users.html",
        title="iRODS - Manage Users",
        users=users,
        groups=groups,
        memberships=memberships,
        protected_groups=PROTECTED_GROUPS,
        zone=irods.IRODS_ZONE,
        embedded=_embedded(),
    )


@irods_admin_bp.route("/irods-admin/users/create", methods=["POST"])
@admin_required
def create_user():
    username = (request.form.get("username") or "").strip()

    if not NAME_RE.match(username):
        flash(
            "Enter a valid username (letters, numbers, ., _, - only) - it must "
            "exactly match the person's Keycloak username.",
            "error",
        )
        return _back()

    home_path = irods.home_collection(username)
    try:
        if irods.user_type(username):
            flash(f"iRODS user '{username}' already exists.", "error")
            return _back()

        irods.create_user(username)
        # Inheritance is on for every folder in the zone; new subfolders get
        # it from their parent, but a new home has none, so set it here.
        irods.set_inheritance(home_path, True, admin=True)
        flash(
            f"Created iRODS user '{username}'. iRODS gave them {home_path} as "
            "their own home folder.",
            "success",
        )
    except IrodsPermissionDenied as exc:
        flash(_needs_rodsadmin(exc), "error")
    except IrodsError as exc:
        flash(str(exc), "error")

    return _back()


@irods_admin_bp.route("/irods-admin/users/delete", methods=["POST"])
@admin_required
def delete_user():
    username = (request.form.get("username") or "").strip()
    if not username:
        flash("No user specified.", "error")
        return _back()

    try:
        irods.remove_user(username)
        flash(f"Removed iRODS user '{username}'. Their files were left in place.", "success")
    except IrodsPermissionDenied as exc:
        flash(_needs_rodsadmin(exc), "error")
    except IrodsError as exc:
        flash(str(exc), "error")

    return _back()


@irods_admin_bp.route("/irods-admin/groups/create", methods=["POST"])
@admin_required
def create_group():
    name = (request.form.get("name") or "").strip()
    if not NAME_RE.match(name):
        flash("Enter a valid group name (letters, numbers, ., _, - only).", "error")
        return _back()

    try:
        irods.create_group(name)
        flash(
            f"Created group '{name}'. Grant it access to a collection once, then "
            "manage who can see that collection by adding and removing members.",
            "success",
        )
    except IrodsPermissionDenied as exc:
        flash(_needs_rodsadmin(exc), "error")
    except IrodsError as exc:
        flash(str(exc), "error")

    return _back()


@irods_admin_bp.route("/irods-admin/groups/delete", methods=["POST"])
@admin_required
def delete_group():
    name = (request.form.get("name") or "").strip()
    if name in PROTECTED_GROUPS:
        flash(f"'{name}' is managed by iRODS and can't be removed here.", "error")
        return _back()

    try:
        irods.remove_group(name)
        flash(f"Removed group '{name}'.", "success")
    except IrodsPermissionDenied as exc:
        flash(_needs_rodsadmin(exc), "error")
    except IrodsError as exc:
        flash(str(exc), "error")

    return _back()


@irods_admin_bp.route("/irods-admin/groups/members", methods=["POST"])
@admin_required
def modify_membership():
    group = (request.form.get("group") or "").strip()
    username = (request.form.get("username") or "").strip()
    action = request.form.get("action")

    if not group or not username:
        flash("Pick both a group and a user.", "error")
        return _back()
    if group in PROTECTED_GROUPS:
        flash(f"'{group}' is managed by iRODS and can't be edited here.", "error")
        return _back()

    try:
        if action == "remove":
            irods.remove_from_group(username, group)
            flash(f"Removed '{username}' from '{group}'.", "success")
        else:
            irods.add_to_group(username, group)
            flash(f"Added '{username}' to '{group}'.", "success")
    except IrodsPermissionDenied as exc:
        flash(_needs_rodsadmin(exc), "error")
    except IrodsError as exc:
        flash(str(exc), "error")

    return _back()
