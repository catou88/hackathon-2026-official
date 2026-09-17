# Four-minute Track 1 demonstration

This is a presentation script and command guide, not a recorded presentation.
Run commands from the repository root with the official development bundle and
a new, empty output directory. Supply the key through the environment; never show
the key or a credentials file on screen.

## 0:00–0:35 — Problem and approach

Our project investigates microservice incidents using the supplied metrics, traces
and logs. Each question gives an incident window and a fault count. The Agent must
identify the requested time, component and reason, and explain the evidence behind
its answer. Our five modules handle bounded data access, metrics and onset,
traces and logs, model routing, and evidence validation with offline evaluation.

## 0:35–1:25 — Run the actual entry point

Build the root Dockerfile ahead of the presentation. Set `RCA_DATA` to the absolute
bundle path and `RCA_OUT` to a new output directory, then run one case:

```bash
docker build -t mantis-rca .
docker run --rm --cpus 2 --memory 8g \
  -e FEATHERLESS_API_KEY -e FEATHERLESS_BASE_URL \
  -v "$RCA_DATA":/data:ro -v "$RCA_OUT":/out \
  mantis-rca python run.py --dataset /data --queries /data/query.csv --out /out --limit 1
```

The judged invocation uses the same three required arguments, without `--limit`.
The default Agent routes within the permitted GLM family. It reads telemetry in
bounded windows, ranks hypotheses and asks a model to select among evidence-backed
candidates. Time and cost limits bound retries and fallback. If the endpoint fails,
the Agent retains a best guess and records the limitation.

## 1:25–2:15 — Inspect the output

Open `predictions.csv` and the evidence file matching its original `row_id`. Walk
through Answer, Confidence, Evidence and Ruled out. Show a source filename, time
window and measured contrast, then explain which alternative remains unresolved.
Do not describe an uncertain alternative as ruled out. The numbers are rendered
from structured observations; model-authored measurements are not accepted.
The complete diagnostic ledger keeps source record locations and replay parameters.

## 2:15–3:20 — Evaluation and model comparison

Show the latest checkpoint table in `REPORT.md`: 20/20 answers, partial accuracy
0.346, strict accuracy 5/20, and 608.960 seconds with two CPUs and eight GiB.
Nineteen workflows completed. One planned Strong upgrade timed out, although the
earlier Flash answer remained available. Distinguish output validity, workflow
completion and correctness: they measure different things.

Show the historical same-agent single-model/routed table and explain its limits.
Both routed responses in that old two-case experiment were empty, so its apparent
score and cost difference does not establish a routing benefit. The latest 20-case
run has real model participation but is not a matched single-model comparison.
The harness freezes cases, configuration and source hashes, retains missing cases
in the denominator and supports repeat runs. Unknown usage remains unknown.

## 3:20–4:00 — Limits and contribution

Correlated resource changes are not necessarily causes. Network subtypes and
multi-fault separation remain difficult; public-development scores do not establish
generalization to the hidden deployment. Six sampled transforms replayed exactly,
but that does not certify every aggregate or every causal explanation.
The project contributes bounded telemetry tools, evidence provenance, GLM routing
and a reproducible evaluation path. Codex generated and revised the implementation
under human direction; runtime model choices and assistant use are disclosed in
the README and report.
