# The trainer the FL SegResNet DAGs launch as a sibling container
# (DockerOperator in dag_fl_segresnet_local.py, docker SDK in
# dag_fl_segresnet_coordinator.py). Built by the fl-segresnet-trainer service
# in airflow/docker-compose.yml -- the tag there must match the DAGs' image=.
#
# Used to build FROM a separate local-only/base-python-gpu image that had to
# be built by hand first. It was only ever this trainer's base, so it is
# folded in here and Compose alone can build the whole thing.
FROM nvidia/cuda:11.8.0-devel-ubuntu22.04

LABEL REGISTRY="local-only"
LABEL IMAGE="fl-segresnet-trainer"
LABEL VERSION="0.1.0"

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-dev \
        git \
        wget \
        curl && \
    ln -sf /usr/bin/python3 /usr/bin/python && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel

# Pinned so every site in a federation trains on the same torch/CUDA build.
RUN python3 -m pip install --no-cache-dir \
    torch==2.5.1 \
    torchvision==0.20.1 \
    --index-url https://download.pytorch.org/whl/cu118

COPY files/requirements.txt /app/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /app/requirements.txt

COPY files/ /app/

WORKDIR /app

CMD ["python3", "-u", "/app/train_fl_round_segresnet.py"]
