"""Host paths this app reads and writes, resolved once from the environment.

The counterpart of API/app/storage_paths.py, and deliberately the same shape:
these two containers exchange work through files, so they have to agree on
where those files are. Both read API_STORAGE_DIR from the root .env, and
docker-compose.yml bind-mounts it at the identical path on both sides.

That sameness is load-bearing rather than tidiness: the API launches sibling
containers through the host Docker daemon, which resolves the paths it is
given against the host filesystem. A path that meant something different in
either place would fail by reading an empty directory rather than by erroring.
"""

import os


def _required(name):
    value = (os.environ.get(name) or '').rstrip('/')
    if not value:
        raise RuntimeError(f'{name} must be set in the root .env')
    return value


# Bulk artifact store shared with the API container.
API_STORAGE_DIR = _required('API_STORAGE_DIR')
