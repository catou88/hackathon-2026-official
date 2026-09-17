# Sources and attribution

The prompts in this directory were newly written with conceptual reference to
Microsoft OpenRCA's Controller/Executor workflow and diagnosis guidance:

- https://github.com/microsoft/OpenRCA/blob/main/rca/baseline/rca_agent/controller.py
- https://github.com/microsoft/OpenRCA/blob/main/rca/baseline/rca_agent/executor.py
- https://github.com/microsoft/OpenRCA/blob/main/rca/baseline/rca_agent/prompt/agent_prompt.py
- https://github.com/microsoft/OpenRCA/blob/main/rca/baseline/rca_agent/prompt/basic_prompt_Market.py

`eval/official_score.py` is copied without algorithm changes from this workspace's
`Track1/starter/score.py`, which vendors OpenRCA's scoring logic. The workspace
starter license is retained as `eval/STARTER_LICENSE.txt`. OpenRCA's MIT notice is below.
This prototype does not include or redistribute the telemetry dataset.

DuckDB documentation: https://duckdb.org/docs/current/data/parquet/overview
Featherless thinking controls: https://featherless.ai/docs/chat-template-kwargs

## OpenRCA MIT License

Copyright (c) Microsoft Corporation.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
