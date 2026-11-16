#!/bin/sh
set -e

# brats launches segmentation algorithms as sibling containers on the host
# docker daemon (docker.sock), so tempfile.mkdtemp() dirs (TMPDIR) must exist
# on the shared API_storage bind mount before the app starts. If this dir is
# missing, Python silently falls back to /tmp, which is private to this
# container and invisible to the host daemon, so the sibling container gets
# an empty input dir and silently produces 0 output files.
mkdir -p "${TMPDIR:?TMPDIR must be set -- see API/docker-compose.yml}"

exec "$@"
