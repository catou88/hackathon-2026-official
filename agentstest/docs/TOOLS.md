# Tools

所有工具经 `Tools.call(name, arguments)` 调用，返回：

```json
{"evidence_id":"ev_...","tool":"...","arguments":{},"ok":true,"data":{},"seconds":0.1}
```

合法调用发生查询失败时返回 `ok:false` 和 error，写入证据账本。
不合法的工具名/参数在执行前拒绝。Schema 通过 `run.py --list-tools` 查看。
返回另含 `queries`：每条 SQL 的 phase、budget_s、seconds、status 与 SQL 哈希；用于区分窗口构建超时和后续查询失败，不发送到模型上下文。
时间使用 `YYYY-MM-DD HH:MM:SS`（UTC+8），区间左闭右开；单次查询最多跨两天。

| 工具 | 主要参数 | 返回内容 |
|---|---|---|
| describe_dataset | 无 | 文件列表、字段类型、大小、时间单位 |
| retrieve_knowledge | query, limit≤6 | 领域卡片与故障原因集合 |
| retrieve_experience | query, limit≤3 | 可选通用调查模式，默认关闭 |
| list_entities | start, end, source | 原始 CMDB ID、节点/实例/服务关系、实际候选名与 component_scopes；支持 trace/log 恢复 |
| metric_anomalies | start, end, sources, top_k≤30 | 全日基线、P05/P95、窗口极值、最早异常、连续性与来源 |
| metric_series | start, end, source, cmdb_id, kpi_name, limit≤240 | 原始序列、相邻差值、采样间隔、来源 |
| log_search | start, end, source, components, keywords, limit≤25 | 日志模板频次、时间范围、原始样本及 log_id |
| trace_summary | start, end, components, limit≤25 | 操作耗时分位数（原始单位）、状态线索、慢 span 标识 |
| trace_edges | start, end, components, limit≤25 | 父子组件关系、开始时间差（毫秒）、样例 trace/span ID |

`source` 只能取固定表名，不接受路径。日志的 source 只允许 log_service/log_proxy。
components 是精确实例 ID 列表，不能把 service 名直接当成 pod 名。
派生服务名只保留为上下文，不进入最终答案候选；Verifier 要求原因类型与节点/容器作用域一致。
metric_series 的 cmdb_id 必须是原始表中的名称，例如 `node-6.shippingservice-1`。

## Python 调用

在 `agentstest` 目录运行，或将该目录加入 Python 模块路径：

```python
from mini_rca.config import Config
from mini_rca.store import TelemetryStore
from mini_rca.tools import Tools

store = TelemetryStore('../Track1/data/Market-cloudbed-1', 'out/tools', Config())
try:
    tools = Tools(store, Config())
    evidence = tools.call('metric_anomalies', {
        'start': '2022-03-20 09:00:00',
        'end': '2022-03-20 09:30:00',
        'sources': ['metric_node', 'metric_container'],
        'top_k': 8,
    })
finally:
    store.close()
```

## 返回信息的限制

- metric_anomalies：基线包含所选日期全文件，可能被其他故障污染；返回异常候选而非诊断。
- metric_series：delta 不是自动推断的计数器速率；负值、重置和单位要另行确认。
- log_search：模板替换数字和 UUID，原始 sample 保留但文本最多 650 字符；默认排序有关键词偏置。
- trace_summary：不把不同操作的正常耗时直接当作故障；应补查同操作基线。
- trace_edges：同 trace_id/span_id 重复记录按最早时间取一条，仅用于关联；跨组件边优先，同组件边仍可返回。
- trace_edges：窗口外父 span 不参与连接，父子开始差包含业务处理、排队和时钟偏差。
- list_entities：名称解析是部署命名约定，不是外部 CMDB 真值。适配其他命名需新增解析器。

所有未转换、未验证的含义都在返回 caveats/note 中传递给 Controller。
