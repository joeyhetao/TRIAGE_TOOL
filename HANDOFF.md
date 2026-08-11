# xlog 交接状态

## 当前轮次

- `round_id`: `cache-layout-readonly-validation`
- 工作树：`/home/melo.liao/worktrees/xlog`
- 分支：`agent/xlog-next`
- 本轮基线提交：`86d9c211a4557187948a429580e0a2de2a5937f4`
- 开始时实际 `git status` 为 clean；旧交接中的 dirty 描述已过期。
- 本轮只修改 xlog，不修改 xvp、xregress、xmanager、xWiki 或 xverif，不 push。

## 合同状态

- bundle 继续使用 `xlog_bundle.v1`、`schema_revision: 1.2`，没有跨组件 contract 变更。
- `case_id` 继续使用相对扫描根的 log 路径，路径稳定且不会因同名 case 冲突。
- case snapshot 继续输出 log、FSDB、daidir、KDB、run manifest、推荐 case 和 alternate case。
- artifact 只使用日志显式引用与配置模板，不递归搜索工程目录。
- `scope_hint` 仍是非权威候选线索；xlog 不输出最终 DUT/environment 根因或 Wiki target。

## 本轮实现

- 标准 UVM report summary 同时出现 `UVM_ERROR : 0` 和 `UVM_FATAL : 0` 时，可作为确定性 PASS 完成证据；非零或不完整 summary 不会判 PASS。
- 日志 artifact 引用会剥离 `FSDB=`、`DAIDIR=`、`RUN_MANIFEST=` 等赋值标签，避免生成带标签的伪路径候选。
- 新增只依赖 Python 标准库的 `scripts/validate_bundle.py`，校验发布的 JSON Schema，并额外要求当前输出为 revision 1.2。
- 新增 Cache 布局 fixture 测试：逐 case log/FSDB、显式 `../../build` 共享 daidir/KDB、manifest、推荐/alternate，以及 artifact 缺失和歧义。

## 真实只读扫描

正式输入：

```text
/home/melo.liao/xvp_smoke_test/ref_dut_validation/third_party_eval/runs/run_20260810_081438/xregress_cache_bug_inputs/xvp_3p_cache
```

执行命令：

```bash
/home/melo.liao/worktrees/xlog/bin/xlog scan \
  --root /home/melo.liao/xvp_smoke_test/ref_dut_validation/third_party_eval/runs/run_20260810_081438/xregress_cache_bug_inputs/xvp_3p_cache/run/test \
  --config /home/melo.liao/xvp_smoke_test/ref_dut_validation/third_party_eval/runs/run_20260810_081438/xregress_cache_bug_inputs/xvp_3p_cache/cfg/xlog_scan.json \
  --output /tmp/xlog-cache-validation-final2.bkynjw/xlog_bundle.json \
  --workers 1
```

结果：

| case_id | status | cluster | log/FSDB/daidir/KDB/manifest |
| --- | --- | --- | --- |
| `baseline_clean_1/baseline_clean_1.log` | pass | none | 全部 resolved |
| `dut_mem_addr_shift_1/dut_mem_addr_shift_1.log` | fail | `error-92538a7c...` | 全部 resolved |
| `dut_rdata_flip_1/dut_rdata_flip_1.log` | fail | `error-aee7ba84...` | 全部 resolved |
| `env_bridge_rsp_flip_1/env_bridge_rsp_flip_1.log` | fail | `error-aee7ba84...` | 全部 resolved |
| `env_cpu_rsp_vif_swap_1/env_cpu_rsp_vif_swap_1.log` | fail | `error-aee7ba84...` | 全部 resolved |

- 汇总：`5 cases / 1 pass / 4 fail / 2 clusters / 2 recommendations / 5 artifact complete`。
- 五个 FSDB 均解析到各自 case 目录。
- 五个 daidir 均解析到共享 `run/build/simv.daidir`。
- 五个 KDB 均解析到共享 `run/build/simv.daidir/kdb.elab++`。
- schema 校验：`valid: ... (xlog_bundle.v1 revision 1.2)`。
- bundle 仅写入 `/tmp`，不提交。
- 扫描前后 `xvp_manifest.txt`、`cfg/xlog_scan.json`、5 个 log 和 5 个 case manifest 的 SHA-256 全部一致；正式 Cache 基准未被修改。

## 测试

```bash
PYTHONPATH=src python3 -m pytest -q
```

结果：`41 passed in 0.29s`。

## 遗留风险

- 当前真实基准只覆盖 `simv.daidir` 共享编译布局；`simv_vip.daidir` 仍依赖同一显式模板机制，尚未用真实 Cache 样本验证。
- artifact snapshot 验证路径、类型、可读性和大小，不校验 FSDB/KDB 内部语义或版本兼容性。
- 没有完整 UVM report summary 且没有配置 PASS marker 的日志，在启用 pass marker 时仍保持 fail，避免把截断日志误判为通过。
- 本轮不实现 LLM、知识路由或 xdebug 调试。

`READY_FOR_INTEGRATION: yes`
