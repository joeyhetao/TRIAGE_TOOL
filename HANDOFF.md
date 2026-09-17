# xlog 交接状态

## 当前轮次

- `round_id`: `primary-case-log-performance-v1`
- 工作树：`/home/melo.liao/worktrees/xlog-primary-case-log-performance-v1`
- 分支：`fix/xlog-primary-case-log-performance-v1`
- 精确锁定集成提交：`39ec9f77c1da29f841fd29848c0002238bc00cee`
- 冻结 xverif 只读提交：`9341b5d42f0f9b6fb634fe568cba0b4b8ebe467b`
- 本轮只修复 xlog 主用例日志选择和单次逐行解析性能，修改 xlog
  代码、测试和本文档；不修改外部仓库或 bundle 合同，不 push。

## 单一 Agent 架构边界

- Codex/Claude Agent 是唯一推理与编排主体。
- xlog 只提供确定性日志发现、状态解析、首错聚类、artifact snapshot 和可选的
  debug recommendation，不执行 xdebug，不作根因判断。
- xregress MCP Lite 负责向 Agent 提供扫描、case 查询和报告保存能力，不自行执行
  debug，也不管理 xverif session。
- Agent 根据证据自主决定是否调用 xverif MCP、选择 recommended 或 alternate case，
  以及实际调查顺序；xlog 不预设固定调查阶段。
- `debug_budget` 保留现有字段和兼容语义，只限制 xlog 输出的推荐 cluster 数量；
  它不是 Agent 执行预算、权限限制、工具调用上限或调查阶段门禁。

## Bundle 合同

- 当前输出：`xlog_bundle.v1`、`schema_revision: 1.3`。
- revision 1.3 新增 `artifacts.manifests`；revision 1.2 中 `resources.run_manifest` 的旧 xvp manifest 语义保持不变。
- 发布 schema 继续接受不含 `artifacts.manifests` 的 revision 1.2 bundle。
- 新输出始终包含两个 manifest descriptor：`xdebug.run_manifest` 和 `xvp.case_manifest`。
- 只有 `resolution_status: resolved`、`parse_status: parsed` 且 `schema_version: xdebug.run-manifest.v1`、`document_state: published` 的 xdebug manifest 才进入 `xdebug_target.run_manifest`。
- 有效旧 xvp manifest 只产生 `selection_status: legacy_fallback`，不会被冒充为 xdebug run manifest。

Manifest descriptor 结构：

```json
{
  "artifact_kind": "xdebug.run_manifest",
  "expected_schema_version": "xdebug.run-manifest.v1",
  "schema_version": "xdebug.run-manifest.v1",
  "path": "/case/xdebug.run-manifest.v1.json",
  "resolution_status": "resolved",
  "parse_status": "parsed",
  "document_state": "published",
  "reason": null,
  "candidates": []
}
```

`artifacts.manifests` 另外包含 `preferred_kind`、`selection_status`、`selected` 和完整 `items`。xlog 只读取 manifest JSON 根对象、schema 和 published state；不验证资源 digest、FSDB/KDB 内容或根因。

## 实现与 Fixture

- xvp case manifest：`artifact_kind: xvp.case_manifest`，期望 `schema: xvp_case_manifest.v1`。
- xdebug run manifest：`artifact_kind: xdebug.run_manifest`，期望 `schema_version: xdebug.run-manifest.v1` 与 `state: published`。
- xdebug manifest 发现顺序：显式日志引用、有效 xvp `external_manifests[]` 引用、`xdebug_run_manifest_templates`；仅使用明确引用和同目录模板，不递归搜索。
- `run_manifest_templates` 继续只表示旧 xvp case manifest；新增 `xdebug_run_manifest_templates`，默认 `{log_dir}/xdebug.run-manifest.v1.json`。
- 新增 `fixtures/manifest_kinds`，覆盖两种 manifest 同时存在、仅旧 xvp、xdebug 缺失、schema 不匹配和路径歧义。
- 已重新生成 `fixtures/rtl_injection_minimal/xlog_bundle.fixture.json`。

## 合同身份

- Bundle schema SHA-256：`10d01d443657db3335a99f68e85a1c9ddd31bdf1ff95ba91fc19ba34d6cf5e87`
- Canonical fixture SHA-256：`e99f650b2d9dadc347d548b9b9d00a750d96bedb434d910ff18b65dd8512e510`

## 测试

```bash
PYTHONPATH=src python3 -m pytest -q
```

结果：`56 passed in 3.02s`。

Canonical fixture 校验：

```text
valid: fixtures/rtl_injection_minimal/xlog_bundle.fixture.json (xlog_bundle.v1 revision 1.3)
```

## 正式 Cache 只读兼容扫描

输入：

```text
/home/melo.liao/xvp_smoke_test/ref_dut_validation/third_party_eval/runs/run_20260810_081438/xregress_cache_bug_inputs/xvp_3p_cache
```

临时输出：

```text
/tmp/xlog-manifest-v2-cache.Rf6D82/xlog_bundle.json
```

结果：`5 cases / 1 pass / 4 fail / 2 clusters / 2 recommendations / 5 artifact complete`，schema revision 1.3 校验通过。

| case | status | manifest compatibility | FSDB/daidir |
| --- | --- | --- | --- |
| `baseline_clean_1` | pass | xvp parsed，xdebug unavailable，`legacy_fallback` | resolved/resolved |
| `dut_mem_addr_shift_1` | fail | xvp parsed，xdebug unavailable，`legacy_fallback` | resolved/resolved |
| `dut_rdata_flip_1` | fail | xvp parsed，xdebug unavailable，`legacy_fallback` | resolved/resolved |
| `env_bridge_rsp_flip_1` | fail | xvp parsed，xdebug unavailable，`legacy_fallback` | resolved/resolved |
| `env_cpu_rsp_vif_swap_1` | fail | xvp parsed，xdebug unavailable，`legacy_fallback` | resolved/resolved |

五个旧 case 的 `xdebug_target` 均保留 FSDB/daidir，但不含 `run_manifest`。扫描前后正式 Cache 三个身份锚点 SHA-256 完全一致，xverif 保持 clean 且 HEAD 不变。

## Typed Cache 五 case 合同验证

新 typed Cache 基准：

```text
/home/melo.liao/worktrees/integration-runs/typed-cache-v1-20260814_003225/generated/xvp_3p_cache
```

使用 `run/test` 作为唯一扫描根并使用 `cfg/xlog_scan.json` 显式 artifact
配置。结果为 `5 cases / 1 pass / 4 fail / 2 clusters / 2 recommendations /
5 artifact complete`，bundle schema revision 1.3 校验通过，全量测试仍为
`43 passed`。

- 五个 case 均同时发现 `xvp.case_manifest` 和
  `xdebug.run_manifest`。
- 五个 case 均选择 `xdebug.run-manifest.v1`，状态为
  `preferred / resolved / parsed / published`。
- 五个 xdebug target 均精确包含逐 case FSDB、共享 daidir 和所选 typed
  run manifest，且 manifest 路径完全一致。
- baseline 为 pass，四个注错 case 为 fail；没有 unclustered failure，
  manifest 选择未出现 legacy fallback、schema mismatch、missing 或
  ambiguity。
- 扫描前后 Cache 文件、目录和符号链接身份摘要完全一致；xlog 与 xverif
  工作树保持 clean。

本次验证 bundle SHA-256：
`19f25119ed66fd019990591f1bf4a31f6b2b11b80df92189f73e56c4be51d57c`。

## 大回归扫描性能修复

- 递归发现现在先检查文件名、`.log` 后缀和 `_bk.log` 排除规则，再对日志
  候选执行 `is_file` 与路径解析；非日志 FSDB 等 artifact 不再进入候选
  `is_file/stat` 路径。
- `parse_log` 在原有单次逐行解析中同时收集 FSDB、daidir、KDB、xvp manifest
  和 xdebug manifest 引用。正常扫描将该映射直接交给 artifact snapshot；
  已扫描但为空的映射不会触发日志回读。
- 正常路径从“每份日志完整读取两次，其中第二次串行”降为“每份日志完整读取一次”，
  全量日志读取次数减少 50%。解析未产出引用集合时保留兼容回退，以维持结构化
  parse error 和 artifact 信息。
- 1550 case synthetic 性能门禁为每 case 一份 log 和同目录 4 GiB 稀疏 FSDB。
  实测扫描 `2.093s`，日志打开 `1550` 次且每份恰好一次，FSDB 内容打开
  `0` 次；测试采用宽松的 `180s` 上限，避免脆弱低阈值。
- 修改前后对仓库固定 `rtl_injection_minimal` fixture 的 bundle 在归一化
  `generated_at` 后完全相等，归一化 SHA-256 为
  `adcce3aaf82b2ffba54235fd5910f81cb9a707e8b44e4887b57ced96bbbdf7a7`。
- `xlog.v1`、`xlog_bundle.v1`、schema revision 1.3、action 数量、输出顺序、
  cluster 和 recommendation 语义均保持不变；未新增进度协议。

## 主用例日志选择与逐行性能修复

- 日志发现仍只接收大小写不敏感的 `.log`，并继续排除
  `*_bk.log`。候选按直接父目录分组：单候选直接保留；多候选时仅在
  “父目录 basename 等于或以后缀匹配 log stem”且结果唯一时选择主日志。
- 匹配大小写不敏感，能够处理目录带额外层级前缀而主日志 stem 较短的
  布局。实现没有硬编码 `rpe_it_hike_`、`novas_dump.log`、
  `tr_db.log`、测试名或公司路径。
- 多日志目录若零匹配或多匹配，继续保留全部候选并按相对路径稳定排序，
  不做猜测；这是兼容旧布局的明确回退语义，不新增 schema 字段。
- 只有选中的主日志进入解析。测试证明真实命名目录中的主日志仅打开一次，
  `*_bk.log` 和两个辅助日志均不打开、不解析，也不生成额外 case。
- 主日志仍完整逐行扫描到 EOF，不做 head/tail sampling，也不把整文件载入
  内存。每行先计算一次小写文本，再以廉价字符串哨兵筛选 artifact、
  simulation time、UVM、VCS、Xcelium、SVA 和配置错误的正则入口；正则仍是
  最终判定。
- 50,001 行以上的合成大日志测试把 UVM error 放在中部，把 PASS、VCS
  report 和总仿真时间放在尾部；结果同时保留中部首错、尾部 PASS 事实和
  `2us` explicit simulation time。
- artifact 引用继续在同一次日志读取中收集，正常路径不回读日志；FSDB
  内容从不打开或哈希。bundle 保持 `xlog_bundle.v1/schema_revision 1.3`，
  schema 和 canonical fixture SHA-256 均未变化。

## 遗留风险

- 1550 case 基准使用 synthetic 小日志与稀疏 FSDB；共享存储延迟、超大真实日志和
  artifact 元数据 `stat` 开销仍需在内网目录上由 xregress 异步扫描观测。
- artifact candidate 元数据检查当前仍在 bundle 组装阶段串行执行。本轮证据显示
  消除日志二次读取已解决主要确定性浪费；若真实测量证明 `stat` 成为下一瓶颈，
  再复用现有 `workers` 做有界并行。
- xlog 不验证 xdebug manifest 中 resource path、size、SHA-256 与实际 FSDB/daidir 是否一致；该严格校验仍由 xdebug 在 session open 前执行。
- revision 1.3 是兼容增加，但 xregress 必须显式消费 `artifacts.manifests`，不能继续把 `resources.run_manifest` 当成 xdebug manifest。
- 路径存在但 JSON 非法、schema 不匹配、state 非 published 或同优先级歧义时，xlog 只报告事实并降级，不猜测替代文件。
- deterministic recommendation 只反映当前排序规则，不能替代 Agent 对证据、
  调查价值和 xverif 调用顺序的判断。
- 编译日志伪 case 是既有已知限制，本轮未修改其发现或分类语义。

## 发布状态

- 远端 `xlog` 发布基线为
  `b457ec8c2ce254f0223aec357f46d46e17075fea`。
- 本轮性能修复已完成本地实现和测试，尚未 push、创建 PR 或 merge。
- bundle schema 与 canonical fixture 合同身份保持不变。

`LOCAL_FIX_READY: yes`
