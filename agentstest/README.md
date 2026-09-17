# Track 1: small evidence-retrieval RCA agent

This submission diagnoses root-cause time, component and failure type from the supplied microservice telemetry. It uses deterministic DuckDB tools for retrieval, GLM for diagnosis, and code for output validation. Only the Controller calls a model; the Executor and Verifier are code.

**The judged three-argument command defaults to the routed agent.** Offline mode is an explicitly selected development baseline. The current revision has 80 passing local tests, including real process-tree termination and deadline recovery on Windows. The public CLI supervises case and run wall-clock limits; Docker resource certification and a repeated live routed/single comparison remain pending. See [REPORT.md](REPORT.md).

## Run the submitted agent

Use this directory as the repository root or export it with `package_submission.py`. Do not submit the surrounding development workspace, which contains other Dockerfiles and datasets.

```bash
docker build -t track1-mini-rca .
docker run --rm --cpus=2 --memory=8g \
  -e FEATHERLESS_API_KEY -e FEATHERLESS_BASE_URL \
  -v /absolute/dataset:/data:ro -v /absolute/output:/out \
  track1-mini-rca \
  python run.py --dataset /data --queries /data/query.csv --out /out
```

Set `FEATHERLESS_API_KEY` in the invoking environment. Set `FEATHERLESS_BASE_URL` to the supplied endpoint, or leave it unset for `https://api.featherless.ai/v1`; do not pass it as an empty string. Real model runs send telemetry summaries to that endpoint and incur costs. The program never automatically loads `.env`. Local authorized runs may explicitly add `--env-file /path/to/.env`.

The image copies only runtime code, prompts and generic knowledge. It excludes development answers, experiment results, credentials and raw telemetry. Dependencies install during image build. Runtime cache, temporary data, predictions and logs go under `--out`. No runtime downloads or extension installation are used. The evaluator supplies network restrictions; Docker alone does not enforce endpoint-only egress.

For local development, install `requirements.txt` into a virtual environment and run:

```bash
python -B run.py --dataset /path/to/bundle --queries /path/to/bundle/query.csv --out out/offline --mode offline
python -B -m unittest discover -s tests -v
python -B eval/validate.py --dataset /path/to/bundle --queries /path/to/bundle/query.csv --out out/validate
```

The validation script defaults to offline shape checks. Its `--live` option is explicit opt-in to a routed model run. Supply a fresh output directory each time.

The CLI accepts `--agent mini_rca.adapter` (default), `agents.routed` (routed alias), and `agents.heuristic` (explicit offline alias). For the upstream validation script, set `VAL_AGENT=mini_rca.adapter` and use the label-free `query.csv`; otherwise its default explicitly requests the offline alias. `mini_rca.adapter.solve(instruction, dataset_dir, ctx)` also exposes the starter's module contract. `ctx` must contain `out_dir`; it can contain `row_id`, `task_index` and a Config. Without row_id, the adapter resolves the exact instruction against the bundle's label-free query.csv.

## Retrieval and model policy

1. Lexically retrieve small domain cards: schemas, failure labels and investigative methods. No embedding service is needed.
2. Query structured metric, log and trace evidence using nine whitelisted tools. CSV is parsed with explicit schemas and time units; SQL is not supplied by the model.
3. Optionally retrieve generic investigation patterns with `--experiences`. These are not labeled development incidents.

Single-failure cases with complete telemetry start on `zai-org/GLM-4.7-Flash`. Multiple faults or missing telemetry start on `zai-org/GLM-4.7`. The fast model can escalate after requesting more evidence or giving a low-confidence answer. This is an uncalibrated cost policy, not a demonstrated difficulty classifier. `--mode single --fast-model MODEL` fixes the model for controlled comparisons. Only the seven competition-listed GLM models are accepted.

There are at most two logical model rounds per case. Retrieval, format repair and low-confidence review share the second round. Each service call has up to three attempts per model, with 0.5/1 second waits; retries stop when the remaining deadline cannot cover the wait and another attempt. Routed mode can then try one same-family fallback. Single mode never changes model. Format errors do not trigger service retries or the service circuit breaker. Persistent service failures open a 60-second circuit after the bounded attempt group; a valid service response clears the failure streak.

The provider was observed returning a standalone JSON answer in `message.reasoning` with empty `content`. The client prefers nonempty content, otherwise accepts only a standalone JSON object from `reasoning_content` or `reasoning`, subject to the same parser and answer verifier. Prose, conflicting side-channel values and truncated replies are rejected. Full provider responses and the selected channel are audited locally.

Model requests run in disposable subprocesses with absolute deadlines. The public `run.py` additionally supervises the entire case worker, including data/tool stalls. Windows uses a Job Object assigned before the worker starts; Linux uses an isolated process group. The supervisor reserves time for termination and artifact writes, checkpoints completed telemetry, owns final output writes, and can recover a grounded fallback and restart for the next case. Case timing includes initial/restart process startup. It refuses to restart if tree cleanup cannot be confirmed. SDK timeout remains an inner limit, not the hard-deadline mechanism. Linux/Docker paths are implemented but not yet resource-certified on this host.

Termination closes local requests but cannot guarantee that the provider stops generation or billing; unknown requests retain cost reservations. Resume restores both run and unfinished-case costs. An already-started interrupted case is finalized from its original-generation snapshot instead of receiving a fresh time allowance; incomplete final artifacts can be repaired without repeating model calls. A forced worker restart drops in-memory DuckDB caches and service circuit state. Direct in-process `adapter.solve` callers receive bounded model transport and interruptible queries; they must supply their own outer case supervision. The public CLI already provides it.

Defaults: 55 seconds/case, 1100 seconds/run, $0.20/case, $20/run, 1200 output tokens, two DuckDB threads and 3 GB DuckDB memory. Competition maximum settings cannot be exceeded via CLI configuration. Fixed competition rates are used, not verified account billing. Explicit provider rejection without usage costs zero in local accounting; unknown transport outcomes retain a conservative reservation. `--resume` preserves prior request costs and counts wall-clock downtime against the original run deadline. It refuses changed queries/configuration.

Names are discovered from the current deployment. Service aliases are context, not valid substitutes for node/container instances. Metric discovery failure can recover names from trace/log telemetry. If no trustworthy component or parseable case window exists, the row is recorded as failed; it does not stop later rows or invent evidence. Such irrecoverable rows still score zero.

## Tools and outputs

```bash
python run.py --list-tools
python run.py --dataset /path/to/bundle --out out/tool --tool trace_edges --tool-args-file examples/trace_tool.json
```

Tools: `describe_dataset`, `retrieve_knowledge`, `retrieve_experience`, `list_entities`, `metric_anomalies`, `metric_series`, `log_search`, `trace_summary`, `trace_edges`.

- `predictions.csv`: stable row_id, ordered JSON fields and the required number of failures.
- `evidence/<row_id>.md`: Answer, Confidence, Evidence, Ruled out.
- `usage.jsonl`: per-model token counts, calls, cost estimates and elapsed time.
- `traces/<row_id>.json`: evidence, routing, validation, adopted answer and difference from the heuristic.
- `model_responses/<row_id>/<attempt>.json`: request context and response content saved before parsing; malformed outputs remain inspectable. The configured API key is redacted. These files can contain telemetry and are excluded from export.
- `run.json`, `summary.json`: configuration, query identity, cumulative budget and artifact-write failures.
- `supervision/`: generation-scoped atomic snapshots, acknowledgements and worker logs; excluded from export. `summary.json` also records outer deadline events.

Module layout: `mini_rca/{cli,adapter,case,store,tools,routing,agents,protocol,verifier,llm,config}.py`; `prompts/`; `knowledge/`; `tests/`; `eval/`.

## Evaluation

```bash
python eval/benchmark.py --dataset /path/to/bundle --queries /path/to/bundle/query.csv \
  --labels /path/to/bundle/dev/query_dev.csv --out out/offline-repeats --repeats 3 --modes offline
```

For an authorized live comparison, use `--modes single routed --live` and optionally `--env-file`. It uses the same retrieval settings, records source hashes, adoption rates, strict/partial accuracy, coverage, dollars/case, seconds/case and sample standard deviations. Labels go only to the separate offline scorer. The default seven development windows are a smoke-test cohort, not a held-out evaluation. Use time-window-grouped splits before tuning or adding experience examples. Pending live results must not be replaced by offline scores.

For all 70 development rows, `eval/full_suite.py --dataset ... --queries ... --labels ... --out ... --live --env-file ...` runs the offline baseline then routed mode in fixed 20/20/20/10 batches, each capped at 1200 seconds. The default $1 live allowance is shared across all four routed batches; unknown reservations count toward it. After budget exhaustion the remaining rows still run with model calls disabled by the cost guard, and the report distinguishes model attempts/adoption from fallback. This is not a claim that all 70 cases fit the competition's 20-case time limit. This single pass also does not measure repeat variability or replace a same-architecture single-model comparison.

Tracked, compact historical and local results are under `eval/results/`. Raw telemetry, provider replies, labels and large local output folders are not packaged.

## Limitations

Global-day quantiles may include other faults. Zero baselines can produce extreme anomaly scores; cumulative metrics and duplicate signals require further study. Trace durations retain unverified source units, and parent-child start gaps are not measured network latency. Evidence-ID validation does not prove causal correctness. The 70-case run completed all cases within the configured case limit, but routed partial was 0.188 versus 0.252286 offline; model answers were adopted in 46/70 cases. No accuracy gain is claimed from reliability fixes. Retrieval experiments remain isolated and are not part of this published baseline. See REPORT.md.

## AI and third-party disclosure

OpenAI Codex generated and revised this agent's code, prompts, tests, packaging and documentation under the user's direction. The user supplied the task, dataset/workspace, constraints and evaluation priorities. No separate human-authored implementation contribution is claimed. Team member identities and student/career status must be supplied by the submitting team in the submission form; they are not inferred here.

Runtime models: the competition-listed GLM family on Featherless; current defaults are GLM-4.7-Flash and GLM-4.7, with GLM-5.3-Flash and GLM-4.6 service fallbacks. The historical comparison also used GLM-5.2. Framework/libraries: custom Python orchestration, DuckDB, OpenAI Python client, jsonschema and python-dotenv; pandas is used by the unchanged official scorer. There is no LangChain or Open Agent Loops runtime dependency in this agent.

Prompts conceptually reference Microsoft OpenRCA; they do not reproduce a vector-RAG implementation. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for sources and licenses.
