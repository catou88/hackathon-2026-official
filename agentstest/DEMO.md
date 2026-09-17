# Four-minute English presentation script

This is a script and recording plan, not a claim that a video has been recorded or submitted.

## 0:00–0:40 — Problem and constraints

“We diagnose when a failure started, which component caused it, and which of the allowed failure types best explains it. The input contains metrics, logs and distributed traces. The evaluation machine has two CPUs, eight gigabytes of memory and no GPU, so we retrieve compact evidence instead of sending raw CSV files to a model.”

Show the Controller → deterministic tools → Verifier flow. Explain that three knowledge layers do not require three model calls.

## 0:40–1:35 — Working system

Run one previously selected case with `--mode offline` for a local-only demonstration, or use the routed default only after authorization for a real endpoint call. Use a new output directory. Label the mode visibly.

Open predictions.csv and evidence/0.md. Show the original filename, UTC+8 window, component, metric baseline and observed anomaly. Say that an anomaly is a candidate, not causal proof. Show the low-confidence statement and alternatives.

## 1:35–2:25 — Routing and reliability

“Complete single-fault evidence starts on Flash. Multiple faults or missing evidence start on the stronger model. A second round is shared by retrieval, format repair and uncertainty review. We preserve original responses, separate content failures from service outages, retry transient service failures with bounded waits, and keep a fallback prediction when possible.”

Show one synthetic regression trace to demonstrate recovery, explicitly labeled as synthetic. Do not present it as a live GLM response. Explain that node/container scope is checked and service aliases cannot replace instance IDs.

## 2:25–3:25 — Evaluation, including negative results

Show REPORT.md and eval/results. The historical seven-case routed run and offline baseline had the same partial score, 0.452857. The routed run cost more and was slower. The Flash-only run suffered protocol failures, so it is not a valid model-quality comparison. State that the current reliability fixes have local validation and offline repeats, while their live accuracy/cost effect remains unmeasured.

Describe the repeatable harness: identical retrieval, a fixed case cohort, separate labels/scoring, model adoption, strict/partial scores, coverage, dollars, seconds and repeat variability. State that seven development windows are not a held-out deployment.

## 3:25–4:00 — Limits and next evidence

“We have not shown that more reasoning improves this dataset. Zero-baseline metric scores, duplicate signals and unverified trace units may limit evidence quality. Next we will validate the fixed output protocol with real responses, run controlled routed/single repeats, and certify the container under the published resource limits.”

Finish by showing the source layout, AI disclosure and outstanding checks. Do not claim that the container was built, the repository published, or the form submitted unless those actions have actually completed.
