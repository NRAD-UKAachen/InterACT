"""Host paths this API reads and writes, resolved once from the environment.

Every module that stores something under API_storage imports from here rather
than spelling out an absolute path, so relocating the checkout is a change to
API_STORAGE_DIR in the root .env and nothing else.

These are deliberately the SAME path inside the container as on the host.
Several routes launch sibling containers through the host Docker daemon
(FastSurfer, BraTS, the screw trainer) and hand them paths they resolved here;
the daemon resolves those against the host filesystem, so a path that meant
something different inside this container would mount an empty directory and
the tool would silently produce nothing. API/docker-compose.yml mounts them
accordingly -- keep the two sides equal.
"""

import os


def _required(name):
    value = (os.environ.get(name) or '').rstrip('/')
    if not value:
        raise RuntimeError(f'{name} must be set in the root .env')
    return value


# Bulk artifact store: model weights, per-task outputs, scratch.
API_STORAGE_DIR = _required('API_STORAGE_DIR')
