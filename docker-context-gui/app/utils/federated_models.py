"""Registry of federated-learning-capable models: which DAG pair backs each
one, for the "which model" choice on the Training Rounds / Instances pages.

One dict, one source of truth, imported by both
federated_instances.py (to render an
allow/revoke toggle per model's local DAG) and federated_rounds.py (to
resolve which DAG pair a job actually triggers).

Adding a new federated model means: build its coordinator + local DAG
pair (see dag_fl_segresnet_*.py as the template), build its processing
container, then add one entry here. Nothing else in the registration/
trust layer changes -- that's all model-agnostic already.
"""

FL_MODELS = {
    "segresnet_auto3dseg": {
        "title": "SegResNet (Auto3DSeg)",
        "description": (
            "MONAI Auto3DSeg's SegResNet algorithm. Round 0 analyzes every "
            "site's data to automatically work out the right preprocessing "
            "and model setup; every site then trains against that shared "
            "setup starting from round 1."
        ),
        "coordinator_dag_id": "fl_segresnet_coordinator",
        "local_dag_id": "fl_segresnet_local",
        "dir_name": "fl-segresnet",
        "icon": "fa-shapes",
    },
}
