# Submission packaging verification — 2026-09-17

Runtime source: `97c759682d72832d4f008fdf13b1d1c4f218f03e` (`wip/rca-model-ranking`),
integrated with main `85ca14eef69681a848e49323ed8a40c3993eb835`. Packaging changes
only clarify the submitted implementation, exclude the historical alternative
from the image and provide the presentation guide. Runtime behavior is unchanged.

## Checks actually executed

- Full `unittest` discovery: **183 tests passed, 34.638 seconds, no skips**, with
  `RCA_TEST_DATA` pointing to the official Market-cloudbed-1 bundle.
- Unmodified official `validate_submission.py`: **two real cases passed, zero
  warnings**, with `VAL_AGENT=agents.routed` and `RCA_MODE=deterministic`.
- `docker build -t mantis-rca-submission:20260917 .`: success from repository root.
- Final image smoke: original rows **0 and 1**, both returned once with nonempty
  predictions and all four required evidence headings. No `--agent` or `RCA_MODE`
  override was supplied; the attempt manifest confirms `agents.routed`.
- Docker enforced **2 CPUs / 8 GiB**, read-only root and data mounts, and
  `--network none`. No key was supplied and there were **zero model requests**.
  Runner-reported case times were approximately 16.1 and 14.7 seconds; these are
  not a new end-to-end performance benchmark.
- All **22 runtime source hashes** in the preceding 20-case manifest match.
  That manifest also records tests: `tests/m1/test_runtime_store.py` differs from
  its historical hash. The fresh 183-test run covers the committed test version;
  this difference does not change the measured runtime source.
- One file named `Dockerfile` remains, at repository root. The separate alternative
  retains `Dockerfile.alternative`; its exporter restores `Dockerfile` when exported
  independently. No tracked `.env`, `query_dev.csv`, private-key, Parquet or pickle
  artifacts were found. A pattern scan found no private-key blocks or matching
  API-token literals; this is not a complete historical secret audit.

Host: macOS / OrbStack; image platform: Linux arm64; container working directory:
`/app`. Local built image ID:
`sha256:e0b6d5ce90657570644eed0676f06940dc2e03cc724c220b9e297ee02f9d16f3`.
The arm64 smoke does not constitute a separate amd64 performance measurement.

## Reproduce

From `track-1/starter`, with dependencies installed:

```bash
RCA_TEST_DATA=/absolute/Market-cloudbed-1 python -m unittest discover -s tests -p 'test_*.py'
```

From the repository root:

```bash
RCA_MODE=deterministic VAL_AGENT=agents.routed python track-1/scripts/validate_submission.py \
  --submission track-1/starter --dataset /absolute/Market-cloudbed-1 \
  --queries /absolute/Market-cloudbed-1/query.csv
docker build -t mantis-rca-submission:20260917 .
docker run --rm --cpus 2 --memory 8g --network none --read-only \
  -v /absolute/Market-cloudbed-1:/data:ro -v /absolute/fresh-output:/out \
  mantis-rca-submission:20260917 \
  python run.py --dataset /data --queries /data/query.csv --out /out --limit 2
```

The earlier [20-case model checkpoint](../workflow-repair-20260917/README.md)
remains the accuracy and live resource measurement. Its one incomplete Strong
escalation, unknown timeout usage, absence of a current matched single-model
comparison and unmeasured repeat variance remain disclosed in the root report.
The English demo script is not a recording. Repository verification does not
establish that the team completed the submission form or presentation.
