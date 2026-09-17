# 本地验证记录

最新结果：80 项程序测试和5项评测工具测试通过。硬超时修复后的离线与在线各70例均完成；在线strict 6/70、partial 0.188、采用46/70，最长54.032秒；离线strict10/70、partial0.252286。模型未带来得分增益。中断resume和CSV落盘边界已追加本地回归修复。详细结果见根目录REPORT.md、eval/results/full70_hard_timeout.json和[HARD_TIMEOUT.md](HARD_TIMEOUT.md)。以下为历史分阶段记录，不代表这些已完成项仍未验证。

日期：2026-09-17。来源：本地 Market-cloudbed-1 开发数据。

## 已完成

- Python 代码编译检查通过。
- 最新 36 项 unittest 通过，包括原有 20 项与本轮 16 项可靠性回归：格式错误不熔断、服务冷却恢复、原始响应记录与 key 脱敏、共享第二轮纠错、说明字段归一化、纠错费用上限、查询阶段超时等。
- 70 条本地 query.csv 均成功解析，得到 57 个不同窗口；其中 26 道双故障题。
- 真实数据 row_id 0、1、6 跑通离线链路，指标、日志、trace 粗筛均返回成功。
- trace_edges 在真实 row_id 0 窗口独立执行成功，结果保存在 `out/trace-tool/tool-result.json`。
- 通过 fake model 验证 Controller 的“请求工具 → 执行 → 最终答案 → 校验”完整路径，没有外部请求。

## 实际离线冒烟运行

路径：`out/smoke-verified/`。命令：

```text
python agentstest/run.py --mode offline --row-ids 0,1,6 --out agentstest/out/smoke-verified
```

| row_id | 模式 | 耗时 |
|---|---|---:|
| 0 | offline-fallback | 6.12 秒 |
| 1 | offline-fallback | 4.95 秒 |
| 6 | offline-fallback | 5.00 秒 |

整轮约 16.09 秒，模型调用 0、费用 $0。无预先转换的 Parquet；此前读过原文件，因此操作系统文件缓存可能是热的。
这些结果仅反映当前电脑与三个案例，不代表 2 CPU/8 GB 评测机器的性能。

独立读取开发答案评分：覆盖率 3/3，strict 1/3，平均 partial 0.5。
这是弱启发式回退成绩，不是 GLM/RAG 的诊断质量结论，也不是留出集验证。
早期 SQL 语法问题已修复；`out/smoke-initial/` 是修复前产物，不作为当前成功验证。

## 尚未验证

- 初次实现时在线测试被自动审批拒绝，用户选择“仅完成本地验证”；之后另次任务已产生首轮真实 A/B 产物，见 out/glm-ab-20260917/REPORT.md。该批模型未带来得分增益。
- 本轮修复后的真实模型接入、routed 相对 single 的准确率、成本或耗时增益；本轮未发起外部请求。
- 全 70 案例诊断、新部署泛化、严格评测容器内的峰值内存和完整 20 例总耗时。
- duration/mrt 单位、所有 KPI 的 counter/gauge 语义、状态码映射的完整性。
- 证据引用目前校验可回查性，尚不自动核验模型每句解释的因果正确性。

建议后续在明确授权在线测试后，固定时间窗口分组与检索配置，做小批 routed/single 对照；不要先增加模型轮数或向量层。

## 可靠性修复后本地复跑

`out/reliability-local-20260917/`：row_id 0、1、4、5、8、9、25，经验层开启。
7/7 完成，49 个工具返回成功，0 个查询超时；整轮 45.797 秒，费用 $0。
row 1 的 trace_summary 用时 8.984 秒，说明本机耗时也有波动，不能据这次成功宣称历史超时已根治。
覆盖率 1.0、strict 2/7、partial 0.452857；与旧无模型基线一致。
旧证据上的新首轮路由回放为 fast 6 / strong 1，不代表实际在线调用比例或节省金额。
核查细节见 [RELIABILITY_REVIEW.md](RELIABILITY_REVIEW.md)。
