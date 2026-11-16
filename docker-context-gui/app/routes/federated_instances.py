"""Federated Learning -- instance registration ("Instances" tab).

The trust handshake: each pairwise relationship needs two tokens
exchanged, one per direction (see federated_store's module docstring for
why -- one token per partner and direction, not a shared secret). Concretely:

  1. Admin on A clicks "Add Remote Instance", enters B's name + base_url.
     A's backend mints a FRESH token for B to use calling A, and shows it
     to A's admin ONCE ("send this to B's admin").
  2. A's admin sends B's admin: A's name, A's base_url, and that token.
  3. B does the same "Add Remote Instance" for A, pasting in the token A
     just generated (as B's outgoing_token for A) -- and B's own backend
     ALSO mints a fresh token for A to use calling B, shown to B's admin
     to send back to A.
  4. A pastes that returned token into B's card ("Update outgoing token"),
     completing the mutual registration.

Also hosts the one machine-to-machine endpoint that has nothing to do with
a specific run: incoming/job, which is how a remote coordinator asks THIS
instance to run one of its own DAGs (used by dag_fl_segresnet_coordinator's
_distribute_round, calling each remote's incoming_job_url).
"""
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, render_template, request

from app.oidc_auth import oidc
from app.utils.airflow_client import AirflowClient
from app.utils.federated_auth import require_instance_token
from app.utils.federated_models import FL_MODELS
from app.utils.federated_store import (
    add_remote_instance,
    delete_instance,
    get_local_instance,
    list_instances,
    rotate_incoming_token,
    update_instance,
    update_local_instance,
)
from app.utils.roles import admin_required

federated_instances_bp = Blueprint(
    "federated_instances", __name__, url_prefix="/federated/instances"
)


def _with_rotation_status(inst):
    """Attaches a `rotation` field for the template's badge/reminder --
    purely a display hint (see rotate_incoming_token's docstring: nothing
    here ever auto-expires a token, so a missed reminder never breaks a
    call by surprise). None for the local row, which has no incoming
    token to rotate."""
    if inst.get("role") != "remote":
        inst["rotation"] = None
        return inst
    issued = inst.get("token_issued_at") or inst.get("created_at")
    reminder_days = inst.get("reminder_days")
    days_since = (datetime.now(timezone.utc) - datetime.fromisoformat(issued)).days if issued else None
    inst["rotation"] = {
        "days_since": days_since,
        "reminder_days": reminder_days,
        "due": reminder_days is not None and days_since is not None and days_since >= reminder_days,
    }
    return inst


@federated_instances_bp.route("/")
@admin_required
def index():
    return render_template(
        "federated_instances.html",
        title="Federated Learning -- Instances",
        instances=[_with_rotation_status(inst) for inst in list_instances()],
        fl_models=FL_MODELS,
        # So the "Training Permissions" chips can show a model's real name
        # (e.g. "SegResNet (Auto3DSeg)") instead of its internal DAG id
        # (e.g. "fl_segresnet_local") -- inst.allowed_dags only ever
        # stores the latter, since that's what actually gets checked when
        # a round starts (see federated_rounds.start_round).
        dag_titles={cfg["local_dag_id"]: cfg["title"] for cfg in FL_MODELS.values()},
    )


@federated_instances_bp.route("/local")
@admin_required
def local_definition():
    """Name + base_url only, for reference when telling a partner admin
    how to address you -- no token here anymore. The actual credential a
    partner needs is minted per-registration (see add_remote below), not
    a single static secret to copy."""
    inst = get_local_instance()
    return jsonify({"name": inst["name"], "base_url": inst["base_url"]})


@federated_instances_bp.route("/local", methods=["PUT"])
@admin_required
def update_local():
    """Fixes the one-shot-at-creation problem: name/base_url otherwise
    freeze at whatever the env vars were the first time this instance's
    record got created (see update_local_instance's docstring) -- e.g.
    the generic "interact-local" default, if FEDERATED_LOCAL_INSTANCE_NAME
    wasn't set before the first visit to these pages."""
    body = request.get_json(force=True)
    allowed_fields = {"name", "base_url"}
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    if not updates:
        return jsonify({"error": "nothing to update -- expected 'name' and/or 'base_url'"}), 400
    inst = update_local_instance(**updates)
    return jsonify(inst)


@federated_instances_bp.route("/", methods=["POST"])
@admin_required
def add_remote():
    """Body: {name, base_url, risk_acknowledged, allowed_dags?,
    their_outgoing_token?}. their_outgoing_token is optional
    -- fill it in later via PUT once the partner shares their token back.

    Response includes incoming_token_for_them: shown ONCE, send it to
    that partner's admin now."""
    body = request.get_json(force=True)

    if not body.get("risk_acknowledged"):
        return jsonify({
            "error": "risk_acknowledged is required -- the admin must confirm they "
                     "understand weight updates can leak information about the "
                     "underlying training data before this instance is registered."
        }), 400

    inst = add_remote_instance(
        name=body["name"],
        base_url=body["base_url"],
        their_outgoing_token=body.get("their_outgoing_token", ""),
        allowed_dags=body.get("allowed_dags", []),
        risk_acknowledged_by=oidc.user_getfield("preferred_username") or oidc.user_getfield("email"),
    )
    return jsonify(inst), 201


@federated_instances_bp.route("/<instance_id>", methods=["PUT"])
@admin_required
def edit_remote(instance_id):
    body = request.get_json(force=True)
    allowed_fields = {
        "allowed_dags", "base_url",
        "outgoing_token", "reminder_days",
    }
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    inst = update_instance(instance_id, **updates)
    return jsonify(inst)


@federated_instances_bp.route("/<instance_id>", methods=["DELETE"])
@admin_required
def remove_remote(instance_id):
    delete_instance(instance_id)
    return "", 204


@federated_instances_bp.route("/<instance_id>/rotate", methods=["POST"])
@admin_required
def rotate_remote(instance_id):
    """Mints a fresh token for this remote to use calling us, invalidating
    the old one immediately -- see rotate_incoming_token's docstring. Same
    one-time-display contract as add_remote: the response's
    incoming_token_for_them must be sent to that remote's admin now."""
    try:
        new_token = rotate_incoming_token(instance_id)
    except KeyError:
        return jsonify({"error": "unknown instance"}), 404
    return jsonify({"incoming_token_for_them": new_token})


@federated_instances_bp.route("/incoming/job", methods=["POST"])
@require_instance_token
def incoming_job():
    """A registered coordinator (g.calling_instance -- correctly resolved
    to exactly that remote's own record, since it presented ITS OWN
    token, see federated_store.find_by_incoming_token) asking THIS
    instance to run one of its own DAGs. Body: {"dag_id": str, "conf": dict}.

    Injects callback_token into conf before triggering: the token THIS
    instance already has on file for calling the caller back (its
    outgoing_token for them) -- so the coordinator never has to invent or
    transmit that credential itself, and the local DAG's every call back
    (fetch model/context, upload result) is authenticated automatically.
    """
    body = request.get_json(force=True)
    dag_id = body.get("dag_id")
    conf = body.get("conf", {})

    allowed = g.calling_instance.get("allowed_dags", [])
    if dag_id not in allowed:
        # Shown verbatim on the coordinator's Training Rounds page (see
        # _check_remote_response in dag_fl_segresnet_coordinator.py), so
        # name the model as the Instances page does, not the DAG id.
        model_title = next(
            (cfg["title"] for cfg in FL_MODELS.values() if cfg["local_dag_id"] == dag_id),
            dag_id,
        )
        caller = g.calling_instance["name"]
        return jsonify({
            "error": f"model '{model_title}' is not allowed for {caller} here. "
                     f"An admin on this site has to click Allow next to it on "
                     f"{caller}'s entry under Federated Learning > Instances."
        }), 403

    conf["callback_token"] = g.calling_instance.get("outgoing_token", "")

    client = AirflowClient()
    result = client.trigger_dag(dag_id, conf=conf, dag_run_id=conf.get("run_id"))
    return jsonify(result), 202
