# SAMinRAD - GPU-Flask-App (SAM2) fuer die InterACT-Plattform.
# Der SAM2-Checkpoint ist NICHT im Image: docker-entrypoint.sh laedt ihn beim
# ersten Start nach /app/checkpoints (Host-Volume). data/ und checkpoints/
# kommen beide als Volume, im Image liegt nur der Code.
FROM pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SAM2_DEVICE=cuda \
    SAM2_CHECKPOINT=/app/checkpoints/sam2.1_hiera_large.pt \
    FLASK_HOST=0.0.0.0 \
    FLASK_PORT=8003 \
    FLASK_DEBUG=False

# Der Container startet als root (siehe docker-entrypoint.sh) und wechselt
# dann zu HOST_UID, der damit kein beschreibbares /root hat; torch/matplotlib
# brauchen aber ein HOME.
ENV HOME=/tmp \
    MPLCONFIGDIR=/tmp/mplconfig

# System-Abhaengigkeiten: git (fuer den SAM2-git-Install), curl (Checkpoint-
# Download im Entrypoint), OpenGL/GLib (bildverarbeitende Libs), gosu (Entrypoint
# startet als root, um Bind-Mounts zu chown'en, und wechselt dann zu HOST_UID)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl libgl1 libglib2.0-0 gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python-Abhaengigkeiten zuerst (bessere Layer-Cache-Nutzung)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt gunicorn

# SAM2 installieren (liefert die Abhaengigkeiten; CUDA-Extension ueberspringen -> kein nvcc noetig).
# torch/torchvision sind im Base-Image bereits vorhanden und werden nicht angefasst.
RUN SAM2_BUILD_CUDA=0 pip install --no-cache-dir \
        "git+https://github.com/facebookresearch/segment-anything-2.git"

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# App-Code + configs/ (checkpoints/ und data/ sind per .dockerignore ausgeschlossen)
COPY . /app

EXPOSE 8003

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Ein Worker (globaler Zustand + ein Modell im Speicher), Timeout aus fuer lange GPU-Ops.
# limit-request-field-size/-fields raised above gunicorn's defaults (8190B /
# 100 fields): the GUI's reverse proxy forwards the browser's request headers
# almost unchanged, and on the hospital domain those can carry more or larger
# headers (other apps' cookies, proxy-added headers) than a from-scratch
# Flask app would ever see, which used to trip "limit request headers fields
# size" and reject the request outright.
CMD ["gunicorn", "--workers", "1", "--threads", "4", "--timeout", "0", \
     "--limit-request-field_size", "16380", "--limit-request-fields", "200", \
     "--bind", "0.0.0.0:8003", "app:app"]
