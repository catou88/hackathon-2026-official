# 五个模块：代码、测试与集成状态

五个模块均已实现并接入 `main`。本目录保存模块规范和入口索引，唯一运行实现位于 `track-1/starter/`。提交历史保留各模块的独立实现、修复及集成记录。

| 模块规范 | 主要实现 | 测试 |
|---|---|---|
| [M1 数据与运行集成](01_contract_data_runtime.md) | [contracts](../../track-1/starter/agents/rca/contracts.py)、[data_access](../../track-1/starter/agents/rca/data_access.py)、[runtime](../../track-1/starter/agents/rca/runtime.py)、[run.py](../../track-1/starter/run.py)、[Dockerfile](../../Dockerfile) | [M1](../../track-1/starter/tests/m1)、[integration](../../track-1/starter/tests/integration) |
| [M2 指标与开始时间](02_metrics_onset.md) | [metrics](../../track-1/starter/agents/rca/metrics.py)、[onset](../../track-1/starter/agents/rca/onset.py) | [M2](../../track-1/starter/tests/m2) |
| [M3 Traces 与日志](03_traces_network_logs.md) | [traces](../../track-1/starter/agents/rca/traces.py)、[network](../../track-1/starter/agents/rca/network.py)、[logs](../../track-1/starter/agents/rca/logs.py) | [M3](../../track-1/starter/tests/m3) |
| [M4 Agent 与路由](04_controller_routing.md) | [routed](../../track-1/starter/agents/routed.py)、[controller](../../track-1/starter/agents/rca/controller.py)、[ranking](../../track-1/starter/agents/rca/ranking.py)、[routing](../../track-1/starter/agents/rca/routing.py)、[prompts](../../track-1/starter/agents/rca/prompts.py)、[llm](../../track-1/starter/llm.py) | [M4](../../track-1/starter/tests/m4) |
| [M5 证据与评测](05_evidence_evaluation.md) | [evidence](../../track-1/starter/agents/rca/evidence.py)、[validation](../../track-1/starter/agents/rca/validation.py)、[eval](../../eval) | [M5](../../track-1/starter/tests/m5) |

默认入口为 `agents.routed`。运行示例、环境变量和安装方法见[根 README](../../README.md)。

提交打包复核的完整测试 **183 项全部通过，34.638 秒，无跳过项**，包含五项真实遥测检查。官方输出格式校验完成两道真实题目，零警告；根 Dockerfile 构建成功，最终镜像在 2 CPU / 8 GiB、只读数据及根文件系统、禁网条件下通过两题默认入口检查。详见[提交验证记录](../../eval/results/submission-ready-20260917/README.md)。

该版本已有[20 题真实模型与容器实测](../../eval/results/workflow-repair-20260917/README.md)：20/20 答案，partial 0.346、strict 5/20，总耗时 608.960 秒，19/20 计划流程完成。一次 Strong 升级超时，保留 Flash 答案。全部 22 个记录的运行源文件哈希与提交版本一致；完整 70 题、新版 matched single-model 对照和重复方差尚未验证。历史 133 项测试及两题模型对照仍保留在 eval/results 中，不与新版结果混用。详见[英文报告及限制](../../REPORT.md)。

密钥、原始数据、虚拟环境及完整大型输出不进入仓库。各模块使用相同的 [rca-v1 接口](../INTERFACES.md)；官方 accuracy evaluator 和 heuristic baseline 保持原实现。
