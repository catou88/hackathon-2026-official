Verifier contract (implemented by Python, not an additional model call):
- Require the exact failure count, valid UTC+8 times within the requested window,
  a discovered component name, and an allowed reason.
- Validate confidence and successful telemetry evidence references.
- Preserve repeated components when separate failures require them.
- Sort by time and emit benchmark fields in datetime, component, reason order.
- Invalid model output, deadline, or unavailable model triggers a clearly labeled
  deterministic best guess with low confidence, not an invented completed diagnosis.
- Structural validation cannot establish causal correctness or calibrate confidence.
