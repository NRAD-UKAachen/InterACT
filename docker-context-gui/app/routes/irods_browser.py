# app/routes/irods_browser.py
"""
File browser for the dockerized iRODS zone, embedded in the GUI sidebar.

Every route forwards the logged-in person's own Keycloak access token to the
iRODS HTTP API (app.utils.irods_client), so all reads/writes/deletes are checked
against that person's real iRODS ACLs - permission errors surfaced here are
genuine iRODS responses, not something this file decides on its own.

Note the "Shared with me" view: iRODS has no path traversal permission, so being
granted read on /zone/home/alice/project does not let you list /zone/home to find
it. A share is therefore invisible to a plain tree browser, and has to be
surfaced with a catalog query instead. See irods_client.shared_with_me.

PHI access is audited inside irods_client, per object, so the routes here add
only the two events that have no single iRODS operation behind them: a folder
downloaded as one ZIP, and an upload the browser drove file by file.
"""
import logging
import os
import time
import zipfile

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    Response, abort, jsonify, stream_with_context,
)

from app import oidc_auth
from app.oidc_auth import oidc
from app.utils import audit
from app.utils import irods_client as irods
from app.utils.irods_client import (
    IrodsAlreadyExists, IrodsError, IrodsNoAccount, IrodsNotFound, IrodsPermissionDenied,
)

irods_browser_bp = Blueprint("irods_browser", __name__, template_folder="templates")
logger = logging.getLogger(__name__)


def _to_irods_path(rel_path):
    """URL path segment (no leading slash) -> absolute iRODS logical path."""
    return "/" + (rel_path or "").strip("/")


def _to_rel_path(irods_path):
    return irods_path.strip("/")


def _embedded():
    return request.values.get("embedded") or None


def _picker():
    """Set when this view is loaded as a folder picker (see batch.html's
    'Browse iRODS...' button, which opens /irods/browse/?picker=1&embedded=1
    in a modal iframe) rather than as the normal file browser. Threaded
    through the same redirects/links as _embedded() so picking survives
    navigating into subfolders or round-tripping through a POST action."""
    return request.values.get("picker") or None


def _back_to(rel_path):
    return redirect(url_for("irods_browser.browse", rel_path=rel_path, embedded=_embedded(), picker=_picker()))


def _sanitize_relpath(relpath):
    """A dropped folder's per-file path -> safe path segments under the target.

    relpath comes from the browser's drag-and-drop entry API, so it's untrusted
    input like any other path from outside the process - strip any "." / ".."
    segment rather than let a crafted drop write outside the upload target.
    """
    return [p for p in relpath.replace("\\", "/").split("/") if p not in ("", ".", "..")]


def _no_account_page(exc):
    """Explain a missing iRODS account without leaving the iRODS section.

    Redirecting to the main home page here looks broken when the GUI has loaded
    this view into its content iframe: home.html renders its own sidebar, so the
    user gets a second navigation panel nested inside the first. Stay put and
    render an explanation instead.
    """
    return render_template(
        "irods_no_account.html",
        title="iRODS",
        username=irods.current_username(),
        no_account=isinstance(exc, IrodsNoAccount),
        detail=str(exc),
        embedded=_embedded(),
    )


@irods_browser_bp.route("/irods/")
@oidc.require_login
def index():
    return redirect(url_for(
        "irods_browser.browse",
        rel_path=_to_rel_path(irods.home_collection(irods.current_username())),
        embedded=_embedded(),
        picker=_picker(),
    ))


@irods_browser_bp.route("/irods/shared")
@oidc.require_login
def shared():
    """Collections other people have granted this person access to."""
    username = irods.current_username()
    try:
        entries = irods.shared_with_me(username)
    except IrodsError as exc:
        # Any failure gets the explanation page, same as browse(). Falling back
        # to an empty list here would render "Nothing has been shared with you
        # yet", which reads as a fact about your permissions rather than as the
        # outage it actually is.
        return _no_account_page(exc)

    return render_template(
        "irods_shared.html",
        title="iRODS - Shared with me",
        entries=entries,
        embedded=_embedded(),
        picker=_picker(),
    )


@irods_browser_bp.route("/irods/browse/", defaults={"rel_path": ""})
@irods_browser_bp.route("/irods/browse/<path:rel_path>")
@oidc.require_login
def browse(rel_path):
    username = irods.current_username()
    home = irods.home_collection(username)
    path = _to_irods_path(rel_path) if rel_path else home

    try:
        # stat first: an unreadable or missing collection raises here, whereas
        # list_collection would just come back empty and render a blank folder.
        stat = irods.stat_collection(path)
        listing = irods.list_collection(path)
    except IrodsNoAccount as exc:
        return _no_account_page(exc)
    except (IrodsNotFound, IrodsPermissionDenied) as exc:
        flash(str(exc), "error")
        # Already at home and still failing: there is nowhere better to send
        # them, and redirecting to index would just bounce back here forever.
        if path == home:
            return _no_account_page(exc)
        return redirect(url_for("irods_browser.index", embedded=_embedded()))
    except IrodsError as exc:
        # Never redirect out of the iRODS section - see _no_account_page.
        return _no_account_page(exc)

    permissions = stat.get("permissions") or []
    # Access can come via a group, and iRODS only ever names the group in the
    # ACL - so match against this person's groups too, or someone who can write
    # through a group gets shown a read-only page.
    my_permission = irods.effective_permission(stat, username, irods.groups_of(username))
    can_write = my_permission in ("modify_object", "write", "own")
    can_share = my_permission == "own"

    breadcrumbs = []
    accum = ""
    for part in path.strip("/").split("/"):
        accum += f"/{part}"
        breadcrumbs.append({"name": part, "rel_path": _to_rel_path(accum)})

    # Only offer ".." when the parent is somewhere this person can actually go.
    # Walking above a shared collection normally lands on a collection they have
    # no access to, and offering a link that always errors is worse than no link.
    parent_rel_path = None
    parts = path.strip("/").split("/")
    if len(parts) > 1:
        parent = "/" + "/".join(parts[:-1])
        if parent == home or path.startswith(f"{home}/"):
            parent_rel_path = _to_rel_path(parent)

    return render_template(
        "irods_browser.html",
        title="iRODS",
        current_path=path,
        rel_path=_to_rel_path(path),
        breadcrumbs=breadcrumbs,
        parent_rel_path=parent_rel_path,
        subcollections=listing["collections"],
        data_objects=listing["data_objects"],
        permissions=permissions,
        can_write=can_write,
        can_share=can_share,
        embedded=_embedded(),
        picker=_picker(),
    )


@irods_browser_bp.route("/irods/mkdir", methods=["POST"])
@oidc.require_login
def mkdir():
    rel_path = request.form.get("rel_path", "")
    name = (request.form.get("name") or "").strip()
    parent_path = _to_irods_path(rel_path) if rel_path else irods.home_collection(irods.current_username())

    if not name or "/" in name:
        flash("Enter a valid folder name.", "error")
        return _back_to(rel_path)

    try:
        irods.create_collection(f"{parent_path.rstrip('/')}/{name}")
        flash(f"Created folder {name}.", "success")
    except IrodsError as exc:
        flash(str(exc), "error")

    return _back_to(rel_path)


def _upload_target(rel_path):
    """The collection an upload posted with this rel_path writes into."""
    if rel_path:
        return _to_irods_path(rel_path)
    return irods.home_collection(irods.current_username())


def _create_upload_dirs(parent_path, dirs_needed):
    """Create every subfolder a folder drop needs, parents first.

    `dirs_needed` is an iterable of path-segment tuples relative to
    parent_path. Sorted shortest-first so a parent collection always exists
    before a child is created inside it. Returns None on success, or the
    message to report on the first failure.
    """
    for dir_parts in sorted(set(dirs_needed), key=len):
        try:
            irods.create_collection(f"{parent_path.rstrip('/')}/{'/'.join(dir_parts)}")
        except IrodsAlreadyExists:
            pass
        except IrodsError as exc:
            return f"Could not create folder {'/'.join(dir_parts)}: {exc}"
    return None


def _flash_upload_summary(succeeded, failed, succeeded_count=None, failed_count=None):
    """Report how an upload went.

    `succeeded` names them only while there are few enough to be worth
    reading. The two *_count arguments override the tallies for a caller that
    counted more than it cared to send the names of - see upload_finish.
    """
    total = len(succeeded) if succeeded_count is None else succeeded_count
    if total:
        if total <= 3 and len(succeeded) == total:
            flash(f"Uploaded {', '.join(succeeded)}.", "success")
        else:
            flash(f"Uploaded {total} files.", "success")

    total_failed = len(failed) if failed_count is None else failed_count
    if total_failed:
        # A folder drop that fails wholesale (no write permission, say) fails
        # once per file, and the whole list would be an unreadable flash
        # message the size of the drop.
        shown = failed[:5]
        extra = total_failed - len(shown)
        message = "Failed to upload: " + "; ".join(shown)
        if extra > 0:
            message += f"; and {extra} more"
        flash(message, "error")


@irods_browser_bp.route("/irods/upload/prepare", methods=["POST"])
@oidc.require_login
def upload_prepare():
    """Create the collections a folder drop needs, before its files are sent.

    The browser uploads one file per request (see irods_browser.html's
    performUpload), several at a time, so there is no single request that can
    still see the whole drop and lay its folders out first. Doing it per file
    instead would mean re-creating the same ancestors once per file and
    racing on them between concurrent uploads; this is one round trip for the
    whole tree instead.
    """
    payload = request.get_json(silent=True) or {}
    parent_path = _upload_target(payload.get("rel_path", ""))

    dirs_needed = set()
    for relpath in payload.get("dirs") or []:
        parts = _sanitize_relpath(relpath)
        if parts:
            dirs_needed.add(tuple(parts))

    error = _create_upload_dirs(parent_path, dirs_needed)
    if error:
        return jsonify({"ok": False, "error": error}), 502
    return jsonify({"ok": True})


@irods_browser_bp.route("/irods/upload/finish", methods=["POST"])
@oidc.require_login
def upload_finish():
    """Flash the summary for an upload the browser drove file by file.

    The per-file requests can't produce it - none of them knows how the drop
    as a whole went - and the page reloads once they're all done, which drops
    anything the JS put on screen. So the browser reports the tally here and
    the same flashes as a single-request upload survive that reload.
    """
    payload = request.get_json(silent=True) or {}
    succeeded = [str(name) for name in (payload.get("succeeded") or [])]
    failed = [str(name) for name in (payload.get("failed") or [])][:50]

    def counted(key, names):
        try:
            return max(int(payload.get(key, len(names))), len(names))
        except (TypeError, ValueError):
            return len(names)

    succeeded_count = counted("succeeded_count", succeeded)
    failed_count = counted("failed_count", failed)

    # The individual writes are already audited by irods_client; this records
    # the drop as one act, which is what a reviewer actually wants to see when
    # someone uploads a whole study folder.
    audit.record(
        "irods", "upload", object=_upload_target(payload.get("rel_path", "")),
        object_type="collection", count=succeeded_count,
        outcome="success" if not failed_count else "error",
        detail=f"folder upload finished: {succeeded_count} written, {failed_count} failed",
    )

    _flash_upload_summary(
        succeeded, failed,
        succeeded_count=succeeded_count,
        failed_count=failed_count,
    )
    return jsonify({"ok": True})


@irods_browser_bp.route("/irods/upload", methods=["POST"])
@oidc.require_login
def upload():
    """Write posted files into the target collection.

    Normally called once per file by the browser's uploader, but it still
    accepts (and writes concurrently) as many files as one request carries,
    which is what the plain form POST in irods_browser.html sends if the JS
    uploader never runs.

    Answers JSON when the caller asks for it - the JS uploader needs a result
    it can count, not a redirect to a page it isn't going to render.
    """
    wants_json = request.form.get("ajax") == "1"
    rel_path = request.form.get("rel_path", "")
    parent_path = _upload_target(rel_path)

    # getlist, not get: the drag-and-drop drop zone and the file picker both
    # submit under the "file" field, but a drag can drop several files at once.
    uploaded_files = request.files.getlist("file")
    # A folder drop sends one "relpath" per "file", in the same order, so the
    # folder's structure can be rebuilt here - the file picker and a plain
    # (non-folder) drop send no "relpath" fields at all, hence the length check.
    relpaths = request.form.getlist("relpath")
    if len(relpaths) != len(uploaded_files):
        relpaths = [None] * len(uploaded_files)

    resolved = []  # (werkzeug FileStorage, [path, segments, ..., filename])
    dirs_needed = set()
    for uploaded, relpath in zip(uploaded_files, relpaths):
        if not uploaded or not uploaded.filename:
            continue
        parts = _sanitize_relpath(relpath) if relpath else []
        if not parts:
            parts = [os.path.basename(uploaded.filename)]
        if len(parts) > 1:
            for depth in range(1, len(parts)):
                dirs_needed.add(tuple(parts[:depth]))
        resolved.append((uploaded, parts))

    if not resolved:
        if wants_json:
            return jsonify({"ok": False, "error": "No file in request."}), 400
        flash("Choose a file to upload.", "error")
        return _back_to(rel_path)

    # Skipped for the JS uploader: it has already laid the whole tree out in
    # one upload_prepare call, and re-deriving the ancestors here would mean
    # a create_collection round trip per ancestor per file, plus concurrent
    # requests racing to create the same collection.
    if not wants_json:
        error = _create_upload_dirs(parent_path, dirs_needed)
        if error:
            flash(error, "error")
            return _back_to(rel_path)

    names = ["/".join(parts) for _uploaded, parts in resolved]
    # Hand the werkzeug streams straight through - no temp file, and nothing
    # buffers a whole file in memory on the way to iRODS. Concurrently,
    # because a data object costs ~25-30 ms in iRODS regardless of its size
    # and writing them one after another was what made large uploads crawl -
    # see irods_client.WRITE_CONCURRENCY.
    results = irods.write_data_objects(
        (f"{parent_path.rstrip('/')}/{name}", (lambda u=uploaded: u.stream))
        for name, (uploaded, _parts) in zip(names, resolved)
    )

    succeeded, failed = [], []
    for name, (_lpath, exc) in zip(names, results):
        if exc is None:
            succeeded.append(name)
        else:
            failed.append(f"{name} ({exc})")

    if wants_json:
        return jsonify({"ok": not failed, "succeeded": succeeded, "failed": failed}), (
            200 if not failed else 502
        )

    _flash_upload_summary(succeeded, failed)
    return _back_to(rel_path)


@irods_browser_bp.route("/irods/download/<path:rel_path>")
@oidc.require_login
def download(rel_path):
    path = _to_irods_path(rel_path)
    filename = path.rsplit("/", 1)[-1]

    try:
        upstream = irods.read_data_object(path)
    except (IrodsNoAccount, IrodsPermissionDenied):
        abort(403)
    except IrodsNotFound:
        abort(404)
    except IrodsError:
        abort(502)

    return Response(
        stream_with_context(upstream.iter_content(chunk_size=64 * 1024)),
        mimetype="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _walk_irods_tree(path, token_box):
    """Recursively list every data object under an iRODS collection as
    (irods_path, [rel_path_parts], size) - mirrors batch.py's
    _list_irods_tree, used here to build a folder's ZIP download
    (download_folder below). A large tree is hundreds of catalog queries, so
    the token is refreshed between collections like the reads are."""
    entries = []

    def walk(coll_path, rel_parts):
        token_box.refresh_if_needed()
        listing = irods.list_collection(coll_path, token=token_box.get())
        for sub in listing["collections"]:
            walk(sub["path"], rel_parts + [sub["name"]])
        for obj in listing["data_objects"]:
            entries.append((obj["path"], rel_parts + [obj["name"]], obj["size"]))

    walk(path, [])
    return entries


# How many times download_folder tries to open one file's read before giving
# up on the whole download. Each attempt refreshes the token first if it is
# near expiry, so a 401 right at the expiry edge gets a second chance.
_FOLDER_READ_ATTEMPTS = 3

# Added to a folder ZIP listing the files that could not be included - ones
# deleted, or not readable by this person, since the listing was taken.
_FOLDER_MISSING_NOTE = "_MISSING_FILES.txt"


class _ZipChunkSink:
    """Write-only file object for zipfile to stream a folder ZIP into.

    It has no tell()/seek(), so zipfile treats it as unseekable and writes
    each entry's sizes in a trailing data descriptor instead of going back to
    patch its header - which is what lets download_folder send the ZIP while
    it is still being built. drain() hands over whatever has been written
    since the last call."""

    def __init__(self):
        self._chunks = []

    def write(self, data):
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def drain(self):
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def _open_folder_entry(obj_path, token_box, actor):
    """Open one file's read for download_folder, retrying a transient
    failure. Not-found / permission errors are raised straight away: those
    are about the file itself, and a retry won't change them."""
    for attempt in range(1, _FOLDER_READ_ATTEMPTS + 1):
        token_box.refresh_if_needed()
        try:
            return irods.read_data_object(obj_path, token=token_box.get(), actor=actor)
        except (IrodsNotFound, IrodsPermissionDenied):
            raise
        except IrodsError:
            if attempt == _FOLDER_READ_ATTEMPTS:
                raise
            time.sleep(attempt)


@irods_browser_bp.route("/irods/download_folder/<path:rel_path>")
@oidc.require_login
def download_folder(rel_path):
    """Download a whole collection as a ZIP - download() above only ever
    handled a single data object, so a study or cohort folder had to be
    pulled down one file at a time through the browser.

    The ZIP is streamed to the browser as it is built, one file at a time
    (the iRODS HTTP API has no bulk/recursive-read operation), so memory use
    stays flat however big the folder is - a cohort can be tens of GB.

    That makes the download run far longer than a Keycloak access token
    lives, so it reads with an oidc_auth.RefreshingToken. Before that, the
    token expired a few minutes in, every remaining read got a 401, and the
    reads were skipped silently - handing back a ZIP missing most of the
    folder that looked complete. Now a file is only left out when it was
    deleted or is unreadable to this person (listed in _MISSING_FILES.txt
    inside the ZIP); any other read failure aborts the stream, so the
    browser reports the download as failed instead of saving a partial ZIP.
    """
    path = _to_irods_path(rel_path)
    folder_name = path.rstrip("/").rsplit("/", 1)[-1] or "download"

    # Captured inside the request, like batch.py's stage-in: the refresh
    # bundle is what keeps the token alive for the length of the download.
    token_box = oidc_auth.RefreshingToken(oidc_auth.token_refresh_bundle() or {
        "access_token": irods.current_access_token(), "expires_at": None,
    })
    actor = irods.current_username()

    try:
        entries = _walk_irods_tree(path, token_box)
    except (IrodsNoAccount, IrodsPermissionDenied):
        audit.record("irods", "export", object=path, object_type="collection",
                     outcome="denied")
        abort(403)
    except IrodsNotFound:
        abort(404)
    except IrodsError:
        audit.record("irods", "export", object=path, object_type="collection",
                     outcome="error")
        abort(502)

    if not entries:
        flash(f"{folder_name} is empty - nothing to download.", "error")
        return redirect(url_for("irods_browser.browse", rel_path=rel_path, embedded=_embedded(), picker=_picker()))

    # Bulk export off the platform: recorded as one event on top of the
    # per-file reads below, because "downloaded a 3000-file cohort as a ZIP"
    # is the fact a reviewer is looking for, and it is not reconstructable
    # from 3000 individual read records that look like ordinary browsing.
    audit.record("irods", "export", object=path, object_type="collection",
                 count=len(entries), detail="folder downloaded as ZIP")

    def generate():
        sink = _ZipChunkSink()
        missing = []
        try:
            with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for obj_path, parts, size in entries:
                    name = "/".join(parts)
                    try:
                        upstream = _open_folder_entry(obj_path, token_box, actor)
                    except (IrodsNotFound, IrodsPermissionDenied) as exc:
                        missing.append(f"{name}\t{exc}")
                        continue
                    info = zipfile.ZipInfo(name, date_time=time.localtime()[:6])
                    info.compress_type = zipfile.ZIP_DEFLATED
                    # Known up front so zipfile switches to ZIP64 for a file
                    # over 2 GB - it can't go back and fix the header later.
                    info.file_size = size
                    with upstream, zf.open(info, "w") as entry:
                        for chunk in upstream.iter_content(chunk_size=256 * 1024):
                            entry.write(chunk)
                            yield sink.drain()
                    yield sink.drain()
                if missing:
                    zf.writestr(
                        _FOLDER_MISSING_NOTE,
                        f"{len(missing)} of {len(entries)} files could not be "
                        "included in this download:\n\n" + "\n".join(missing) + "\n",
                    )
            yield sink.drain()
        except Exception:
            # Headers are already sent, so there is no error page to show -
            # raising cuts the response short, and the browser marks the
            # download as failed rather than saving a ZIP that looks whole.
            logger.exception("Folder download of %s aborted", path)
            audit.record("irods", "export", object=path, object_type="collection",
                         outcome="error", detail="folder ZIP download aborted")
            raise

    return Response(
        stream_with_context(generate()),
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{folder_name}.zip"',
            # Don't let a reverse proxy buffer the whole ZIP before passing
            # it on - the point is that it streams.
            "X-Accel-Buffering": "no",
        },
    )


@irods_browser_bp.route("/irods/delete", methods=["POST"])
@oidc.require_login
def delete():
    rel_path = request.form.get("rel_path", "")
    target = request.form.get("target", "")
    entry_type = request.form.get("type")  # "collection" or "data_object"

    if not target:
        flash("Nothing selected to delete.", "error")
        return _back_to(rel_path)

    target_path = _to_irods_path(target)
    try:
        if entry_type == "collection":
            irods.remove_collection(target_path, recurse=True)
        else:
            irods.remove_data_object(target_path)
        flash(f"Deleted {target_path}.", "success")
    except IrodsError as exc:
        flash(str(exc), "error")

    return _back_to(rel_path)


@irods_browser_bp.route("/irods/share", methods=["POST"])
@oidc.require_login
def share():
    """Let the owner of a collection or data object share it directly.

    iRODS already allows the owner of an entry to grant access to it, so
    routing this through an admin (as the old admin-only page did) was a
    self-imposed restriction, not an iRODS one.

    "target" is any collection or data object under the folder being browsed
    (defaults to the browsed folder itself, for the toolbar's own "Share"
    button) - this one route backs the share icon next to every row as well
    as the one for the current folder.
    """
    rel_path = request.form.get("rel_path", "")
    entity = (request.form.get("entity") or "").strip()
    level = request.form.get("level", "read")
    entry_type = request.form.get("type", "collection")  # "collection" or "data_object"
    target = request.form.get("target") or ""
    target_path = _to_irods_path(target) if target else _to_irods_path(rel_path)

    if not entity:
        flash("Choose a person or group to share with.", "error")
        return _back_to(rel_path)
    if level not in irods.PERMISSION_LEVELS:
        flash("Invalid permission level.", "error")
        return _back_to(rel_path)

    name = target_path.rsplit("/", 1)[-1]
    try:
        covered_contents = True
        if entry_type == "data_object":
            irods.set_data_object_permission(target_path, entity, level)
        else:
            # Recursive: a grant on the collection alone shows the grantee an
            # empty folder. Inheritance then covers files added later.
            covered_contents = irods.share_collection(target_path, entity, level)

        if not covered_contents:
            flash(
                f"{name}'s name contains characters that can't be used in a "
                f"server-side rule, so only the folder itself was changed, not "
                f"what is already inside it. Ask an admin to run "
                f"ichmod -r on it.",
                "warning",
            )
        if level == "null":
            flash(f"Removed {entity}'s access to {name}.", "success")
        else:
            flash(
                f"Shared {name} with {entity} ({level})."
                + (" Everything already inside it is included." if entry_type != "data_object" and covered_contents else "")
                + (" New files added here will be shared too." if entry_type != "data_object" else ""),
                "success",
            )
    except IrodsError as exc:
        flash(str(exc), "error")

    return _back_to(rel_path)


