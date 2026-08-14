# xlog 交接状态

## 当前轮次

- `round_id`: `manifest-kind-v2-adapter`
- 工作树：`/home/melo.liao/worktrees/xlog-manifest-v2`
- 分支：`agent/xlog-manifest-v2`
- 起始提交：`ff2c5263a51c02bcefe10c467d0c959b3a6e9fcd`
- 冻结 xvp 生产提交：`8b2971b77cce80b991e77d83cfab492405385410`
- 冻结 xverif 只读提交：`9341b5d42f0f9b6fb634fe568cba0b4b8ebe467b`
- 本轮只修改 xlog 独立仓库，不修改 xvp、xregress、xmanager、xWiki 或 xverif，不 push。

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

结果：`43 passed in 0.36s`。

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

## 遗留风险

- 正式 Cache 是旧 xvp manifest 基准；新 xvp `8b2971b` 的双 manifest 输出已用合成 fixture 覆盖，但尚未对一个新生成的完整 VCS Cache run 做真实扫描。
- xlog 不验证 xdebug manifest 中 resource path、size、SHA-256 与实际 FSDB/daidir 是否一致；该严格校验仍由 xdebug 在 session open 前执行。
- revision 1.3 是兼容增加，但 xregress 必须显式消费 `artifacts.manifests`，不能继续把 `resources.run_manifest` 当成 xdebug manifest。
- 路径存在但 JSON 非法、schema 不匹配、state 非 published 或同优先级歧义时，xlog 只报告事实并降级，不猜测替代文件。

`READY_FOR_INTEGRATION: yes`
