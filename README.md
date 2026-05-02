# slawatch

Checks SLA compliance for Cloud Run services by pulling real metrics from
Cloud Monitoring and evaluating them against Google's actual contractual
definition, not a rough approximation. Outputs markdown and JSON reports,
exits non-zero on a breach, so it fits naturally into a daily cron or CI job.

The longer write-up explaining the design choices, trade-offs, and how
this would scale beyond Cloud Run lives in [WRITEUP.pdf](./WRITEUP.pdf).

## What it does

- Pulls `run.googleapis.com/request_count` and `run.googleapis.com/request_latencies`
  from Cloud Monitoring for each configured Cloud Run service.
- Applies the Cloud Run SLA's actual definition of Downtime: a minute counts
  as down when the 5xx infrastructure-error rate exceeds 1% AND there are
  at least 100 valid requests in that minute.
- Computes monthly uptime as `(total minutes - downtime minutes) / total minutes`,
  per the SLA wording, not a rough approximation.
- Compares each result to two thresholds: the published SLA floor (Google's
  contractual commitment) and the team's own SLO (a stricter internal target).
- Reports error-budget consumption in seconds and percent so on-call can
  see how much headroom is left before the SLO is at risk.
- Identifies the financial-credit tier the team would qualify for if Google
  fell short, with a reminder that customers must request the credit within
  30 days.

## Quick start

The fastest way to run is Docker, no Python setup needed.

```bash
git clone <repo>
cd slawatch

# Authenticate with GCP (one-time)
gcloud auth application-default login

# Run against the live project
docker build -t slawatch .
docker run --rm \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v $(pwd)/reports:/app/reports \
  -v ~/.config/gcloud/application_default_credentials.json:/app/adc.json:ro \
  -e GOOGLE_APPLICATION_CREDENTIALS=/app/adc.json \
  slawatch check --config /app/config.yaml
```

Or run directly with pip:

```bash
pip install .
gcloud auth application-default login
slawatch validate --config config.yaml   # check config without hitting Cloud Monitoring
slawatch check --config config.yaml
```

The project and service are already configured in `config.yaml` pointing at
`slawatch-live-demo` / `slawatch-demo` in `us-central1`. See `.env.example`
for the credential paths.

## Config

A full example with all supported fields is in
[`examples/config.example.yaml`](./examples/config.example.yaml).
The shape is:

```yaml
project: slawatch-live-demo
eval_window: 30d
output:
  formats: [markdown, json]
  directory: ./reports
fail_on_breach: true
targets:
  - name: payments-api
    kind: cloud_run
    service: payments-api
    region: us-central1
    slo:
      availability: 0.9999
      latency_p99_ms: 800
    sla:
      gpu: false
      zonal_redundancy: true
```

A few notes on the schema:

- `eval_window` accepts `60s`, `5m`, `1h`, `7d`, `30d`. Thirty days is the
  natural unit because Google's SLA is monthly.
- `slo.availability` is the team's internal target, ideally stricter than
  the published SLA floor. The tool emits a note when it isn't.
- `slo.latency_p99_ms` is optional. When present, the report includes the
  observed p99 over the window and flags a warning if it breaches.

## Authentication

The Cloud Monitoring client uses Application Default Credentials. The
three setups it's been used with:

1. Local development: `gcloud auth application-default login`.
2. CI: GitHub OIDC via Workload Identity Federation (no key files).
3. Local Docker: mount the ADC file or a service-account JSON file with
   `GOOGLE_APPLICATION_CREDENTIALS` pointing at it. The service account
   needs only the `roles/monitoring.viewer` role.

## Running in CI

A reference GitHub Actions workflow is included at
[`.github/workflows/sla-check.yml`](./.github/workflows/sla-check.yml).
It runs daily, validates the config, and uploads the report files as build
artifacts. The live Cloud Monitoring check runs when `GCP_WIF_PROVIDER` and
`GCP_SLAWATCH_SA` are set as repo variables.

For Workload Identity Federation, the
[`google-github-actions/auth`](https://github.com/google-github-actions/auth)
README covers the setup. Set `GCP_WIF_PROVIDER` and `GCP_SLAWATCH_SA` as
repo variables, no keys to rotate.

## Docker

```bash
docker build -t slawatch .

# With Application Default Credentials (local dev)
docker run --rm \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v $(pwd)/reports:/app/reports \
  -v ~/.config/gcloud/application_default_credentials.json:/app/adc.json:ro \
  -e GOOGLE_APPLICATION_CREDENTIALS=/app/adc.json \
  slawatch check --config /app/config.yaml

# With a service account key (CI / production)
docker run --rm \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v $(pwd)/reports:/app/reports \
  -v $(pwd)/sa-key.json:/app/sa-key.json:ro \
  -e GOOGLE_APPLICATION_CREDENTIALS=/app/sa-key.json \
  slawatch check --config /app/config.yaml
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All targets passing |
| 1 | At least one target in WARN state |
| 2 | At least one target BREACHING the SLA floor |
| 3 | Configuration or runtime error |

A non-zero exit fails the CI run, so a breach shows up as a red job in
whatever notification system the team already has.

## Tests

```bash
pip install -e '.[dev]'
pytest -v
```

Covers the compliance math: downtime detection, error budget, region-aware
SLA floors, latency thresholds. No GCP credentials needed, runs offline.

## Extending to other services

The metric fetcher is an abstract class. Adding Cloud Storage or Compute
Engine means one new file implementing `MetricFetcher.fetch`, the evaluator
and reporters stay untouched. Cloud Storage uses per-method success rates from
`storage.googleapis.com/api/request_count` but the SLA structure is the same:
a contractual floor with credit tiers and a team SLO on top.
