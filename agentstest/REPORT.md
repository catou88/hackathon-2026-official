# Track 1 report: small structured-evidence RCA

## Problem and implementation

Given a 30-minute telemetry window and a specified number of failures, the agent produces the requested root-cause time, node/container instance and allowed reason label. It reads the provided deployment rather than retrieving development answers.

The retrieval layers are small lexical domain cards, DuckDB telemetry queries, and optional generic investigation patterns. Nine deterministic tools replace runtime code generation. A Controller chooses a competition-listed GLM, an Executor runs schema-checked tools, and a Verifier checks answer count, time, component scope, reason labels and telemetry evidence references. Only the Controller is an LLM role. OpenRCA inspired the staged workflow; this is not a reproduction of its implementation or a vector database architecture.

The official three-argument CLI defaults to routed mode. Node/container names retain their observed scope; service aliases remain context. Per-row parsing and recoverable artifact-write failures do not stop later cases. Metric entity discovery can recover from trace/log telemetry. Completely invalid inputs or entirely unavailable telemetry still fail explicitly, without inventing a component or timestamp.

At most two logical rounds share retrieval, output repair and uncertainty review. Service faults receive bounded same-model retries with increasing waits, then a same-family fallback in routed mode. Content errors do not trip a service circuit. Original responses are recorded before parsing; secrets and HTTP headers are not included in the submission artifact. Default budgets are below the competition caps. Resume includes durable prior attempt costs and elapsed time, including downtime.

## Historical model comparison — before reliability/submission fixes

Seven different development windows were chosen as the first example of each task: row IDs 0, 1, 4, 5, 8, 9, 25. This is a smoke-test cohort, not a held-out test. The experience layer was enabled, thinking disabled, and output limited to 1200 tokens. A used GLM-4.7-Flash alone. B used the old Flash/GLM-5.2 route with GLM-4.6 as fallback.

| Configuration | Strict cases | Partial mean | Mean case seconds | Total estimated dollars | Adopted model answers |
|---|---:|---:|---:|---:|---:|
| Original starter heuristic | 0/7 | 0.071429 | 1.104 | 0 | N/A |
| Same RAG, offline heuristic | 2/7 | 0.452857 | 5.154 | 0 | N/A |
| A: Flash single model | 2/7 | 0.452857 | 7.734 | 0.00125534 | 0/7 |
| B: old routed policy | 2/7 | 0.452857 | 31.821 | 0.1488162 | 5/7 |

A made only two requests, both unparseable, before the old circuit incorrectly skipped the model. It is not a valid seven-case measurement of Flash's diagnostic capability. B made nine GLM-5.2 and five GLM-4.6 requests, with no Flash calls. Two cases failed optional alternatives validation; one trace query timed out. B's submitted predictions matched the offline baseline case by case. No added model accuracy was observed. Costs use the competition rate table, not account billing.

The original starter also had a timezone bug, making the same-RAG offline baseline the cleaner model-ablation comparison. The original experiment was single-run, so live-model repeat variability is unknown. These numbers are preserved as historical evidence, not claims about the revised route or transport. Machine-readable summaries are in `eval/results/historical_ab.json`.

## Current local verification

- 80 unit/integration tests passed, including real request/case process deadlines, descendant cleanup, checkpoint recovery, unfinished-case cost preservation, provider side-channel JSON compatibility, retry backoff/fallback, node/container scope and output contracts. Five additional synthetic evaluation-harness tests passed.
- The unmodified upstream submission validator passed two real-data cases with `VAL_AGENT=mini_rca.adapter`, zero warnings. The child environment deliberately omitted credentials, so this proves entry/output compatibility through model-unavailable fallback, not live endpoint or model-quality validation.
- Three offline repeats on the seven-case cohort completed 21/21 cases, with no model calls or fees. Mean strict score was 2/7; partial score was 0.452857; coverage was 1.0. All score standard deviations were zero, as expected for deterministic fallback on fixed data.
- Mean full-process seconds per case was 5.46281; the sample standard deviation of the three run-level per-case means was 0.33993 seconds. Individual full runs took 36.438, 37.344 and 40.937 seconds. This statistic is not per-request latency variance.
- These current repeats used the default disabled experience layer; the earlier A/B enabled it. They are separate experiments and should not be merged into one controlled live comparison.
- No prebuilt Parquet was used. OS file caches were not cleared. Results describe this Windows host; they do not certify a 2-CPU/8-GB container.

See `eval/results/offline_repeats.json`, `eval/results/offline_manifest.json` and `eval/results/validation.json` for bounded evidence and validation status. The source hashes in the manifest identify the code used for the repeated run.

## Authorized live smoke test and compatibility fix

On 2026-09-17, rows 0 and 1 were tested with the revised default route, experiences disabled, thinking disabled and 1200 output tokens. The route selected GLM-4.7-Flash for row 0 and GLM-4.7 for row 1. All four initial responses had empty content; the two-round format repair did not trip the service circuit. Both cases fell back, with partial mean 0.75 and strict 1/2, matching the same two offline cases. Live case times were 83.391 and 74.828 seconds, versus 6.297 and 5.250 seconds offline. The 55-second case and 150-second run settings were exceeded: SDK request timeouts are not strict wall-clock deadlines.

A small telemetry-free probe established that the provider placed the JSON answer in `message.reasoning` while leaving content empty. The client now accepts only standalone JSON from reasoning/reasoning_content when content is empty, records the selected channel and full provider response, and retains all answer validation. A subsequent row-0-only routed retest adopted a Flash answer in one round: 12.344 seconds, $0.00063640, partial 0.5 and strict 0. Its scored prediction equaled the offline prediction. This is transport/contract success, not evidence of an accuracy improvement or general latency improvement; repeated same-version trials are still needed.

Known usage across the live diagnosis attempts and successful diagnostic probe estimates $0.01068549 using competition rates. Failed sandbox connections and a probe whose response reader failed retain another $0.02400193 of conservative unknown-cost reservations, for $0.03468742 combined accounting. These are not verified account charges. Compact results and version hashes are in `eval/results/live_smoke.json`; raw telemetry and provider responses remain local under out/ and are excluded from export.

## Subsequent hard-deadline fix

The public CLI now supervises case and run wall-clock deadlines independently of SDK I/O timeouts. Each model request runs in a disposable subprocess. A persistent case worker is owned by a Windows Job Object before execution, or an isolated process group on Linux. Completed telemetry is checkpointed atomically; only the supervisor writes final case artifacts. A hung case is stopped, pending costs remain reserved, and subsequent cases can continue with a restarted worker. Inner request deadlines precede the outer cutoff so ordinary timeouts can recover without restarting the data cache.

On Windows, a synthetic permanently blocked tool was stopped around 3.44 seconds with a 4-second case limit; its grandchild exited, its $0.031 synthetic pending reservation was retained, and the next case completed. A 3-second run limit overrode an 8-second case allowance and returned around 2.72–2.75 seconds. These are real process tests, not mocked elapsed clocks. Direct `adapter.solve` integrations still need their own outer case supervisor; the judged public CLI already includes it.

The full 70-case development comparison uses fixed code/configuration and four batches of 20/20/20/10 for each mode: offline followed by routed, 55 seconds/case and 1200 seconds/batch. Live batches share $0.99 plus a separate preliminary smoke check capped at $0.01. Budget-exhausted cases continue as fallback and remain in the denominator. This single development pass is not a 70-case-under-20-minutes claim or a repeated single-model comparison. Results and the exact evaluated source hashes are recorded in `eval/results/full70_hard_timeout.json`. The subsequent interrupted-resume and Windows CSV replacement fixes have local regression coverage; the full online run used the preceding hard-timeout implementation.

## Completed 70-case development comparison

| Metric | Same RAG offline | Routed GLM agent |
|---|---:|---:|
| Nonempty predictions and complete evidence | 70/70 | 70/70 |
| Strict correctness | 10/70 (14.29%) | 6/70 (8.57%) |
| Mean official partial score | 0.252286 | 0.188000 |
| Adopted model answers | 0/70 | 46/70 (65.71%) |
| Mean case seconds | 4.733 | 28.351 |
| P95 / maximum case seconds | 7.071 / 7.250 | 54.031 / 54.032 |
| Known token-based cost estimate | $0 | $0.15908948 |
| Unknown-outcome reservations | $0 | $0.48668154 |
| Combined conservative accounting | $0 | $0.64577102 |

No case exceeded 55 seconds in the recorded timings; independent artifact checks found no missing, duplicated, empty or malformed predictions. Outer cutoff/cleanup timings are recorded separately from the CSV timing, which precedes artifact commit. Four routed batch processes took 1992.547 seconds in total: this is not a claim that all 70 fit the official 20-case/20-minute allowance.

There were 98 audited requests, 74 parsed responses and 46 final adopted answers (39 Flash, four GLM-4.7 and three GLM-4.6 fallback). Eleven answer-contract rejections concerned failure count (seven), evidence references (two) or reason labels (two); no JSON-format failure was recorded. Twenty-four requests hit an inner or outer hard cutoff, and 14 cases needed outer recovery. Thirteen cases requested more telemetry and then hit the case deadline in the second model round. Adoption establishes structural validity, not causal correctness.

The routed partial score fell by 0.064286 relative to the independent offline run. Only one case improved and six worsened. Unknown reservations are conservative budget accounting, not verified provider charges. The preliminary one-case smoke added $0.001479275 in separately tracked accounting, outside this comparison's $0.64577102. Retrieval optimization is being evaluated separately; it is not part of these online scores or this baseline release.

## Remaining checks and limitations

Docker CLI is unavailable on the development host. A single-root Dockerfile and an allowlisted export are provided, but image build, read-only mounted data, endpoint-only egress, peak whole-process memory and a full 20-case limited-container run have not been verified. A Dockerfile is not a resource certification.

The earlier response-channel smoke reran only one case; the later full-development experiment is documented separately and must not be mixed with those earlier counts. Repeat variability and routed-versus-single accuracy/cost changes remain unmeasured. Hard deadline tests are verified on Windows, not yet in Linux/Docker. Forced worker restarts preserve costs but discard in-memory caches and circuit state. The harness supports paid experiments only with explicit `--live` opt-in. Public repository publication, team information, presentation recording and submission form completion remain external handoff tasks; the English presentation script is not a recorded demo.

Global-day thresholds may be contaminated by other failures. Zero baselines and duplicate counters can dominate ranking. A pgfault excursion does not establish a memory-load injection. Trace duration units and some counter/gauge semantics remain unverified. Parent-child start gaps include processing and clock skew. Evidence IDs establish provenance, not causal truth. Offline alternatives are intentionally cautious and often cannot exclude competing roots. Service/instance naming extraction still needs testing on other deployments.

## Evaluation plan

After the full development smoke, run the same retrieval configuration in single and routed modes across repeated, time-window-grouped held-out cases, and verify container limits. Report strict and partial scores, coverage, dollars/case, seconds/case, model adoption, model mix and sample standard deviations. Count missing predictions as zero and retain failures in denominators.

Keep subsequent retrieval experiments separate: counter deltas, zero-baseline handling, duplicate KPI evidence and trace baselines should each have a fixed comparison. Do not interpret the larger model's agreement with a weak heuristic as independent causal confirmation.

## Contribution and disclosure

The user supplied objectives, local data, constraints and evaluation priorities. OpenAI Codex generated and revised the implementation, prompts, tests, packaging, evaluation scripts and documentation. Runtime libraries and models are listed in README.md. The official scoring implementation and required license notice are retained. No development labels, original telemetry, API keys or provider response dumps are copied into the runtime image.

Judging weights are governed by the participant agreement: technical execution 40%, innovation 30%, potential impact 20%, presentation/demo 10%. Conflicting 35% evidence/20% accuracy statements in starter prose are not presented here as confirmed weighting.
