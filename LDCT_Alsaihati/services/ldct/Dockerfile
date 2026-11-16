FROM mambaorg/micromamba:1.5.1-focal-cuda-12.2.0

# Switch to root to create user
USER root

# Create user
ARG USER_ID=1000
ARG GROUP_ID=1000
RUN addgroup --gid $GROUP_ID appuser && \
    adduser --disabled-password --gecos "" --uid $USER_ID --ingroup appuser appuser

WORKDIR /app
RUN chown appuser:appuser /app
COPY --chown=appuser:appuser environment.yml .

# Install conda environment as appuser
USER appuser
RUN micromamba create -n astra_env -f environment.yml && micromamba clean --all --yes

SHELL ["micromamba", "run", "-n", "astra_env", "/bin/bash", "-c"]

COPY --chown=appuser:appuser . .

EXPOSE 8000

# Default to running the API server, but allow CLI usage
ENTRYPOINT ["micromamba", "run", "-n", "astra_env", "python", "-m", "ldct.api"]
