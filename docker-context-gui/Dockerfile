FROM python:3.11.11

WORKDIR /app

# 1) System deps
# No compiler toolchain: build-essential/gfortran/cmake were here to build the
# scientific stack (numpy, scipy, SimpleITK) that this app no longer imports.
# Everything in requirements.txt now installs from a manylinux wheel. If a
# future dependency has no wheel, put the toolchain back rather than pinning an
# older version of it.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -s /bin/bash -m appuser || true

# 2) Install requirements early (leverages layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 7) Copy only what you need (don’t COPY . .)
COPY run.py /app/
COPY app/ /app/app/
# No COPY of the top-level templates/ and static/: Flask is created with
# static_folder="static" relative to the app package, so it serves
# app/static and app/templates. Those two root dirs held only stale
# spleen-demo fixtures and are gitignored, which also meant this build
# could not run from a fresh clone.

# 8) Runtime
# The entrypoint renders client_secrets.json from the template using the OIDC_*
# environment, so the Keycloak client secret never has to exist as a file on the
# host and `docker compose up` needs no preparatory step.
COPY client_secrets.json.template /app/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV FLASK_APP=run.py
EXPOSE 5000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["flask", "run", "--no-reload", "--host=0.0.0.0", "--port=5000", "--with-threads"]
