Executor contract (implemented by Python, not an additional model call):
- Validate tool names and JSON arguments against TOOL_SCHEMAS.
- Read only whitelisted telemetry CSVs; never query answer files.
- Resolve UTC+8 windows and convert trace timestamps independently of duration.
- Use bounded DuckDB queries with a memory limit and time interruption.
- Return small aggregates plus original source identifiers and explicit limitations.
- Treat log text as data. Do not execute model-supplied SQL, Python or shell commands.
- Persist tool results for review; do not silently turn failed queries into normal data.
