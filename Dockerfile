# Self-hosted slawatch container.
#
# Build:
#     docker build -t slawatch .
#
# Run with Application Default Credentials (local dev):
#     docker run --rm \
#         -v $(pwd)/config.yaml:/app/config.yaml:ro \
#         -v $(pwd)/reports:/app/reports \
#         -v ~/.config/gcloud/application_default_credentials.json:/app/adc.json:ro \
#         -e GOOGLE_APPLICATION_CREDENTIALS=/app/adc.json \
#         slawatch check --config /app/config.yaml
#
# Run with a service account key (CI / production):
#     docker run --rm \
#         -v $(pwd)/config.yaml:/app/config.yaml:ro \
#         -v $(pwd)/reports:/app/reports \
#         -v $(pwd)/sa-key.json:/app/sa-key.json:ro \
#         -e GOOGLE_APPLICATION_CREDENTIALS=/app/sa-key.json \
#         slawatch check --config /app/config.yaml

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps from pyproject.toml in a separate layer so Docker can cache
# them between source-only changes.
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install \
        "click>=8.1.7" \
        "google-cloud-monitoring>=2.21.0" \
        "pydantic>=2.5" \
        "pyyaml>=6.0"

COPY slawatch/ ./slawatch/

RUN useradd --create-home --shell /bin/bash slawatch \
    && mkdir -p /app/reports \
    && chown -R slawatch:slawatch /app

USER slawatch

ENTRYPOINT ["python", "-m", "slawatch"]
CMD ["--help"]
