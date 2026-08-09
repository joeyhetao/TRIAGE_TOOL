# xlog 交接状态

## 当前轮次

- `round_id`: `phase1-contract-integration`
- 组件职责：失败事实、错误簇、推荐/备选 case 与 case 级 artifact snapshot。
- Git 仓库：`/home/melo.liao/ai_tools/xlog`
- 分支：`xlog`
- 基线提交：`5dbf2ceb313ea8b634685277331a143b7cf93bae`
- 当前实现包含未提交修改；本文件不代表已经 commit 或 push。

## 已完成

- `xlog_bundle.v1` 当前输出 `schema_revision: 1.2`。
- 已实现 path-independent `portable_signature` 和非权威 `scope_hint`。
- 已输出 recommended/alternate case 及其 artifact snapshot。
- 已提供最小 RTL 注错 fixture：`fixtures/rtl_injection_minimal/`。
- xlog 只提供失败事实与提示，不决定根因、最终 Wiki target 或知识发布。

## 对外合同

- Schema：`schemas/xlog_bundle.v1.schema.json`
- 真实 fixture：`fixtures/rtl_injection_minimal/xlog_bundle.fixture.json`
- xregress 应保存原始 bundle，并将 `scope_hint` 视为建议而不是路由事实。
- artifact 缺失、歧义和 alternate 只作为事实输入；是否改选 alternate 由 xregress Agent 决定。

## 验证

- `PYTHONPATH=src python3 -m pytest -q`
- 最近结果：`34 passed`

## 已知风险

- 当前 1.2 实现仍处于 dirty working tree，尚未形成可引用的新提交。
- xregress 有一项测试仍硬编码期望 `schema_revision == 1.1`，属于消费者测试滞后。

`READY_FOR_INTEGRATION: yes`
