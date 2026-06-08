# Bug 记录文档

供软件工程师排查和参考历史问题。

---

## BUG-032 Cadence Xcelium 标准报错专用 pattern：开箱即用，含 *SE / source location 全字段

**发现日期**：2026-06-03
**状态**：已修复

### 背景

BUG-031（VCS）落完后，IC 验证另一大事实标准 **Cadence Xcelium**（含旧版 NC-Verilog / Incisive 工具链）仍未被识别。Xcelium 跟 UVM / VCS 结构完全不同，需要独立 pattern：

```
工具前缀: *severity,message_id (file,line|column): description
```

跟 VCS 的核心差异：
- Xcelium 必有**工具前缀**（`xrun:` / `xmsim:` / `xmelab:` 等）
- severity 在**中间**（`*E`/`*W`），不是行首字面量
- ID 紧跟逗号、**不含连字符**（跟 VCS `[CNST-CIF]` 不同）
- source location 可能含 column（`(file,line|column)`）

### 形态调研（WebSearch + 真实 GitHub 样本）

| 样本 | 来源 |
|---|---|
| `xmsim: *W,DSEM2009: This SystemVerilog design is simulated as per IEEE 1800-2009 ...` | [cocotb#1363](https://github.com/cocotb/cocotb/issues/1363) |
| `xmelab: *E,MBXNYI (/wrk/.../riscv_random_stall.sv,87\|20): 'unpacked structure ...'` | [core-v-verif#11](https://github.com/openhwgroup/core-v-verif/issues/11) |
| `xmelab: *E,DLCSMD ...` / `xmelab: *E,CUVMUR ...` | [cva6#2136](https://github.com/openhwgroup/cva6/issues/2136) |
| `xrun: *E,VLGERR ...` | [riscv-dv#305](https://github.com/google/riscv-dv/issues/305) |
| `*F,INTERR` / `*SE,JGUSOS` | [Cadence FV blog](https://community.cadence.com) |

### 关键发现

1. **`*SE` 是双字符 severity**（Severe Error）—— 朴素的 `\*[EWFNS]` 单字符正则会漏掉，必须用 `\*[A-Z]+`。这是调研中最重要的修正点。
2. **工具前缀可带版本/PID 后缀**：`xrun(64): 18.03-s006 ...` —— 版本行**不带 `*severity`**，不会误判为 error；正则用 `(?:\(\d+\))?` 兼容
3. **source location 可能含 column**：`(file,line|column)`，column 部分可选。正则用 `(?:\|\d+)?`
4. **跟 UVM 的交互**：Xcelium 跑 UVM testbench 时，UVM_X 行**不会被 `xmsim:` 包**，仍是 IEEE 1800.2 标准格式 —— 走 UVM 路径，跟 Xcelium pattern 不冲突

### 形态枚举

| # | 变体 | 覆盖策略 |
|---|---|---|
| 1 | `xmsim: *E,XYZID: msg`（主格式） | `_XCELIUM_PATTERN` 命名组覆盖 |
| 2 | `xmelab: *E,XYZID (file,N): msg`（带 source location） | 可选 `(file,line)` 组 |
| 3 | `xmelab: *E,XYZID (file,N\|col): msg`（含 column） | `(?:\|\d+)?` 列号可选 |
| 4 | `xrun(64): *E,...`（带 PID/版本后缀） | `(?:\(\d+\))?` 兼容 |
| 5 | `xrun: *SE,XYZID: msg`（**双字符 severity Severe Error**） | `\*[A-Z]+` 多字符覆盖 |
| 6 | `xmsim: *N,XYZID: msg` / `*I,...`（Note/Info） | `_XCELIUM_LEVEL_MAP` 过滤掉，不计错误 |
| 7 | 9 种工具前缀（xrun/xmsim/xmelab/xmvlog/xmverilog/xmsd/ncsim/ncelab/ncvlog/irun） | 正则 `(?:xrun\|xmsim\|...)` 全覆盖 |

### 修复方案

[core/log_parser.py:51-87](log_analysis/triage_tool/core/log_parser.py#L51-L87) 新增 `_XCELIUM_PATTERN` + `_XCELIUM_ANY` + `_XCELIUM_LEVEL_MAP`：

```python
_XCELIUM_PATTERN = re.compile(
    r'^(?P<tool>xrun|xmsim|xmelab|xmvlog|xmverilog|xmsd|ncsim|ncelab|ncvlog|irun)'
    r'(?:\(\d+\))?'                                          # 可选 (PID/版本)
    r':\s*\*(?P<level>[A-Z]+),'                              # *severity, 多字符
    r'(?P<id>[A-Z][A-Z0-9_]*)'                               # ID（无连字符）
    r'(?:\s*\((?P<file>[^,)]+),(?P<line>\d+)(?:\|\d+)?\))?'  # 可选 (file,line|col)
    r':\s*(?P<msg>.*)',
    re.IGNORECASE
)
_XCELIUM_LEVEL_MAP = {'E': 'ERROR', 'W': 'WARNING', 'F': 'FATAL', 'SE': 'ERROR'}
```

[core/log_parser.py:225-262](log_analysis/triage_tool/core/log_parser.py#L225-L262) 主循环加入第四段（UVM → VCS → **Xcelium** → 通用关键词），命中处理逻辑与 VCS 对齐（statistics 累加、all_errors 去重、WARNING 仅统计不进 top_errors、抽出 file/line 进 `location` 字段）。

续行检测加入 `_XCELIUM_ANY.match()` 排除，防止 Xcelium 行被前一条 pending 误并。

### 关键设计取舍

1. **`*SE` 归并到 `ERROR` 统计**：Severe Error 跟 Error 都是失败标志，UI 不分两个分类，统一计入 `ERROR`。如未来需要细分，第三轮格式库可通过 `level_map` 区分。
2. **9 种工具前缀全部内置**：包含旧版 NC-Verilog 系列（ncsim/ncelab/ncvlog），覆盖客户可能仍在使用的 legacy 环境。Xcelium 主前缀 `xrun` 和子工具 `xmsim`/`xmelab` 是当前主流。
3. **ID 字符集 `[A-Z][A-Z0-9_]*`（无连字符）** —— 跟 VCS `[A-Z][A-Z0-9_-]*` 区分，符合实证样本：`DSEM2009` / `MBXNYI` / `DLCSMD` / `VLGERR` / `INTERR` / `JGUSOS` 全部无连字符。
4. **冒号是强约束**：Xcelium 真实输出 ID 后必有 `:`（如 `*E,VLGERR: msg` 或 `*E,VLGERR (file,N): msg`），正则用 `: ` 锚定避免误匹配。测试样本必须按真实格式写。

### 跨格式隔离

新增 `test_cross_isolation_three_simulators_in_one_log` 验证 UVM / VCS / Xcelium 三种格式同时存在时各走各路径，各自独立计数：
- UVM 行 → `UVM_ERROR` 统计 + 保留 file/line/timestamp/reporter
- VCS 行 → `ERROR` 统计 + `error_id` 抽取
- Xcelium 行 → `ERROR` 统计（VCS + Xcelium 合并到同一 ERROR 字段，自然汇总）+ `error_id` 抽取 + `location` 字段

### 回归测试

新增 8 条 `test_xcelium_*`：
1. `test_xcelium_zero_config_recognition` —— 零配置识别（核心价值）
2. `test_xcelium_severity_SE_double_char` —— `*SE` 双字符 severity 关键回归
3. `test_xcelium_with_source_location` —— `(file,line)` 和 `(file,line|col)` 两种 location
4. `test_xcelium_all_tool_prefixes` —— 8 种工具前缀（xrun/xmsim/xmelab/xmvlog/ncsim/ncelab/ncvlog/irun）
5. `test_xcelium_pid_version_suffix` —— `xrun(64):` 带后缀
6. `test_xcelium_severity_filter_skips_note_info` —— `*N`/`*I` 不计入错误
7. `test_xcelium_id_routes_to_kb_step1` —— 端到端 KB Step1 精确匹配
8. `test_cross_isolation_three_simulators_in_one_log` —— UVM/VCS/Xcelium 三方隔离

**47/47 通过**（原 39 + 新 8）。

### 同步文档

- [PRD.md](log_analysis/triage_tool/PRD.md) 新增 "Cadence Xcelium 仿真器报错支持" 子条目
- 后续计划：BUG-033 做报错格式库重构，把 UVM/VCS/Xcelium/EXTRA_PATTERNS 抽进 `error_formats.json`，让未来新仿真器格式可以编辑配置不改代码

---

## BUG-031 Synopsys VCS 标准报错专用 pattern：开箱即用、零配置

**发现日期**：2026-06-03
**状态**：已修复

### 背景

UVM（BUG-029）+ EXTRA_PATTERNS 通用关键词（BUG-030）覆盖完后，IC 验证两大事实标准之一的 **Synopsys VCS** 仍未被一等公民对待 —— 用户必须先把 `ERROR` / `WARNING` 等关键词配进 `EXTRA_PATTERNS` 才能识别 VCS 报错，且 VCS 特有的 ID 字符集（含连字符如 `CNST-CIF` / `VPI-CT-NS`）能被通用关键词路径勉强抽出，但**整个识别链路依赖配置、不开箱即用**。

跟 UVM / Xcelium 并列，VCS 应该有专用 pattern。

### 形态调研（WebSearch + 真实 GitHub 样本）

通过 GitHub issue 抓取的真实 VCS 报错样本：

| 样本 | 来源 |
|---|---|
| `Error-[DPI-UED] C++ Exception detected`（含 2-space 缩进续行） | [chipyard#914](https://github.com/ucb-bar/chipyard/issues/914) |
| `Error-[SFCOR] Source file cannot be opened` | [SpinalHDL#669](https://github.com/SpinalHDL/SpinalHDL/issues/669) |
| `Warning-[RT_UO] Unsupported option` | chipyard#914 |
| `Warning-[VPI-CT-NS] VPI function is not supported` | SpinalHDL#669 |
| `Warning-[DBGACC_REG] Unrecognized '-debug_region' option` | SpinalHDL#669 |
| `Error: "../src/.../ibex_top.sv", 982: ibex_simple_system...: at time 18` | [ibex#1645](https://github.com/lowRISC/ibex/issues/1645) |

### 形态枚举（已枚举完备性表）

| # | 变体 | 覆盖策略 |
|---|---|---|
| 1 | `Error-[ID] msg`（主格式） | `_VCS_PATTERN` 命名组覆盖 |
| 2 | `Warning-[ID] msg` / `Fatal-[ID] msg` | 同上 |
| 3 | `Note-[ID] msg` / `Info-[ID] msg` | 同上但**不计入错误统计**（IC 验证语境非错误） |
| 4 | 含续行（2-space 缩进） | 复用 UVM 同款 indented 续行策略 |
| 5 | ID 含连字符（`VPI-CT-NS` / `DPI-UED`） | 字符集 `[A-Z][A-Z0-9_-]*` |
| 6 | **Assertion failure 子格式** `Error: "file", N: hierarchy: at time T` | 暂不覆盖，留待第二阶段（实际触发频次低、结构差异大；当前 EXTRA_PATTERNS 的 `Error` 关键词可兜底） |

### 修复方案

[core/log_parser.py:32-50](log_analysis/triage_tool/core/log_parser.py#L32-L50) 新增 `_VCS_PATTERN` + `_VCS_ANY`（续行检测排除） + `_VCS_LEVEL_MAP`（severity → 内部统计字段映射）：

```python
_VCS_PATTERN = re.compile(
    r'^(?P<level>Error|Warning|Fatal|Note|Info)'
    r'-\['
    r'(?P<id>[A-Z][A-Z0-9_-]*)'
    r'\]\s*'
    r'(?P<msg>.*)',
    re.IGNORECASE
)
_VCS_LEVEL_MAP = {'ERROR': 'ERROR', 'WARNING': 'WARNING', 'FATAL': 'FATAL'}
```

[core/log_parser.py:135-181](log_analysis/triage_tool/core/log_parser.py#L135-L181) 主循环加入三段式 **UVM → VCS → 通用关键词**，VCS 命中处理逻辑与 UVM 对齐（statistics 累加、all_errors 去重、WARNING 仅统计不进 top_errors、FATAL/ERROR 进 top_errors 流水线）。

续行检测加入 `_VCS_ANY.match()` 排除，防止 VCS 行被前一条 pending 误并。

### 关键设计取舍

1. **零配置开箱即用** —— VCS pattern 不依赖 `EXTRA_PATTERNS`，用户上传 VCS 日志即识别。这是 VCS 作为"一等公民"的核心价值。
2. **statistics 字段命名跟 EXTRA_PATTERNS 同集合**（`ERROR` / `WARNING` / `FATAL`，无 `VCS_` 前缀） —— 自然汇总，跨格式不分离统计；UI 端无需为 VCS 单独建分类。
3. **Note/Info 不计入错误统计** —— IC 验证语境下 `Note` / `Info` 是版本/配置信息（如 `Note-[VERSION]`），不应被统计为"错误"。这跟 UVM_INFO 不计入 statistics 的设计哲学一致。
4. **Assertion failure 子格式（`Error: "file", N:` 形态）暂不覆盖** —— 结构与主格式差异大，实际触发频次低（SVA 通常走 UVM 报错路径），当前 EXTRA_PATTERNS 的 `Error` 关键词能兜底；如未来用户实测高频，再加 `_VCS_ASSERT_PATTERN` 作为 VCS pattern 的子规则。
5. **不动 KB schema / `_valid_levels()`** —— 默认 EXTRA_PATTERNS 已含 `ERROR` / `FATAL` / `WARNING`，用户回写 VCS 错误时下拉框已有合法 level 选项，零额外配置。

### 跨格式隔离

[tests/test_log_parser.py](log_analysis/triage_tool/tests/test_log_parser.py) 加两条隔离测试：

- `test_cross_isolation_vcs_does_not_eat_uvm` —— UVM 行优先 UVM 路径（保留 file/line/timestamp/reporter），不被 VCS 正则误吃
- `test_cross_isolation_extra_keywords_does_not_double_count` —— 用户配 `EXTRA_PATTERNS=['ERROR']` 时 VCS 行只计一次（VCS pattern 命中后 `continue`，通用关键词不再触发）

### 回归测试

新增 8 条 `test_vcs_*`：
1. `test_vcs_zero_config_recognition` —— 零配置识别（核心价值）
2. `test_vcs_all_severities` —— 五种 severity 全覆盖（Error/Warning/Fatal 计入；Note/Info 不计）
3. `test_vcs_id_charset_with_hyphens_and_underscores` —— ID 字符集（`VPI-CT-NS` / `DBGACC_REG` / `DPI-UED` / `SE-LMHW`）
4. `test_vcs_continuation_indented` —— 2-space 缩进续行（chipyard#914 实证）
5. `test_vcs_warning_not_in_top_errors` —— WARNING 仅统计不进 top_errors
6. `test_vcs_id_routes_to_kb_step1` —— 抽出 error_id 端到端走 KB Step1 精确匹配
7. `test_cross_isolation_vcs_does_not_eat_uvm` —— UVM 优先级保护
8. `test_cross_isolation_extra_keywords_does_not_double_count` —— 不被通用关键词双计数

**39/39 通过**（原 31 + 新 8）。

### 同步文档

- [PRD.md](log_analysis/triage_tool/PRD.md) 新增 "Synopsys VCS 仿真器报错支持" 章节
- 计划项：BUG-032 将做 Cadence Xcelium 同款覆盖；BUG-033 将做格式库重构（把 UVM/VCS/Xcelium/EXTRA_PATTERNS 全部抽进 `error_formats.json`）

---

## BUG-030 EXTRA_PATTERNS 通用关键词正则放宽：覆盖 VCS / IP / SVA 真实场景，新增 [ID] 抽取

**发现日期**：2026-06-03
**状态**：已修复

### 现象

用户实测一条 VCS 标准报错：

```
Error-[CNST-CIF] Constraints inconsistency failure
```

既不命中 `_UVM_PATTERN`（非 UVM 前缀），也不命中 `_gen_pattern`（行首 `Error-` 后跟 `-` 不是 `:`）。BUG-028 / BUG-029 完备覆盖 UVM 后，这是另一类大面积漏检场景。

### 背景：v1.8 设计意图被 PRD 措辞误导

PRD 第 64 行原文是"匹配 `^关键词: 描述内容` 格式（行首+冒号）"，措辞像是给 `$display("ERROR: ...")` 这种 print 调试用的。但用户（IC 验证工程师）澄清：

> IC 验证场景里**没人用 `$display`** 报错。UVM 平台报错走 `uvm_error` 机制（已被 `_UVM_PATTERN` 覆盖）。"额外错误关键词" 当初设计就是为了覆盖：
> 1. **VCS 标准报错**（`Error-[CNST-CIF] msg`，连字符分隔）
> 2. **IP 内部报错**（vendor-specific，常带 `[ID]`）
> 3. **RTL SVA 报错**（用户自定义，唯一共性是关键字在行首）

这三类**都不带冒号**，分隔符各异。原正则因为 `\s*:\s*` 把它们全部拦在外面，跟真实业务诉求完全错位。

### 根因

[core/log_parser.py:38-45](log_analysis/triage_tool/core/log_parser.py#L38-L45) 旧实现：

```python
return re.compile(r'^(' + alts + r')\s*:\s*(.*)', re.IGNORECASE)
```

两处过严：
1. **`\s*:\s*` 强制冒号** —— 切掉 VCS 的 `-`、SVA 的纯空格、IP 的紧贴 `[`
2. **关键词后无 word boundary** —— 理论上可能误判（虽然 `:` 兜底，但放宽后必须显式守护）

而且 `[ID]` 信息完全没抽出来，命中后 `error_id` 一律硬填 `''`，导致 KB 永远只能走 Step2 关键词匹配，无法利用 VCS 那些稳定的 `[CNST-CIF]` / `[T_BUS_ERR]` 做 Step1 精确匹配。

### 修复方案

[core/log_parser.py:38-58](log_analysis/triage_tool/core/log_parser.py#L38-L58) 重写正则，[第 138-167 行](log_analysis/triage_tool/core/log_parser.py#L138-L167) 把抽出的 ID 灌入 `error_id`：

```python
return re.compile(
    r'^(' + alts + r')\b'                # G1: 关键词 + word boundary（防 Erroring 半匹配）
    r'[\s:\-]*'                          # 任意分隔符（空白/冒号/连字符，0+ 个）
    r'(?:\[([^\]]+)\])?'                 # G2: 可选 [ID]
    r'\s*(.*)',                          # G3: 描述
    re.IGNORECASE
)
```

兼容性矩阵：

| 输入行 | 命中 | level | error_id | description |
|---|---|---|---|---|
| `ERROR: msg`（旧格式兼容） | ✓ | `ERROR` | `''` | `msg` |
| `Error-[CNST-CIF] Constraints...`（VCS） | ✓ | `ERROR` | `CNST-CIF` | `Constraints...` |
| `IP_FATAL[T_BUS_ERR] timeout...`（IP） | ✓ | `IP_FATAL` | `T_BUS_ERR` | `timeout...` |
| `MY_SVA signal X stayed low`（SVA） | ✓ | `MY_SVA` | `''` | `signal X stayed low` |
| `Erroring something`（半匹配） | ✗ `\b` 阻断 | — | — | — |
| `MY_ERR_VAR = something`（赋值） | ✗ `_VAR` 是 word 续接 | — | — | — |

### 设计哲学澄清

借这次修复明确："关键词"和"ID"是**两个维度**：

- **关键词 = 粗类 level**（用户配置，进 `state.EXTRA_PATTERNS`），如 `ERROR` / `IP_FATAL` / `MY_SVA`
- **`[ID]` = 细类 ID**（解析时抽取，进 `error_id` 字段），如 `CNST-CIF` / `T_BUS_ERR`

由此带来的不动决策：
- **不动** `blueprints/config_bp.py` 关键词字符集 `^[A-Z0-9_ ]+$` —— 用户不应该把 `Error-[CNST-CIF]` 整串配成关键词，那是细类信息，应由解析器抽出
- **不动** `state._valid_levels()` —— 关键词字面量 = level 名的硬绑定保持，KB 错误类型列继续显示干净的关键词
- **不动** `_UVM_PATTERN` —— 与 UVM 路径完全隔离

### 回归测试

[tests/test_log_parser.py](log_analysis/triage_tool/tests/test_log_parser.py) 新增 7 条 `test_gen_*` 测试覆盖：VCS 格式 / IP 格式 / SVA 无分隔符 / word-boundary 防误报 / 旧冒号格式兼容 / 同日志混合多格式 / 抽出 ID 端到端走 KB Step1 精确匹配。**31/31 通过**（原 24 + 新 7）。

### 同步文档

- [PRD.md](log_analysis/triage_tool/PRD.md) 第 64 行 "额外错误关键词" 描述按新格式重写
- 项目 memory 落地：v1.8 真实设计意图是 VCS/IP/SVA，下次不再走弯路

---

## BUG-029 UVM 报错行正则完备性升级：覆盖全部 11 种合法变体

**发现日期**：2026-06-02
**状态**：已修复

### 背景

BUG-028 修完参数化 class 名后，用户给出第二条仍然漏检的真实样本：

```
UVM_ERROR @ 82.00ns: uvm_test_top.env.vsqr@@nic_pb_apb_seq [uvm_test_top.env.vsqr.nic_pb_apb_seq] Response queue overflow, response was dropped
```

这条不带 `file(line)` 前缀（从 sequence/vsequencer 报错时 reporter 没传 `__FILE__`/`__LINE__`），现有正则仍漏检。说明前两次修复都是"打地鼠"——只补单点，没系统梳理 UVM 报错行的全部形态。本次做完备性升级。

### 形态梳理（IEEE 1800.2 default report server + 常见自定义 server）

`uvm_default_report_server::compose_report_message` 的输出模板：

```
{sev_string, verbosity_str, " ", filename_line_string, "@ ",
 time_str, ": ", report_object_name, context_str, " [", id, "] ", msg_body_str, terminator_str}
```

各字段的合法变体：

| # | 变体 | 示例 | 触发条件 |
|---|---|---|---|
| 1 | 标准完整 | `UVM_ERROR /tb/dut.sv(42) @ 100ns: comp [ID] msg` | 默认 |
| 2 | 缺 file(line) | `UVM_ERROR @ 82ns: comp [ID] msg` | sequence/vsequencer 报错；filename 为 `""` |
| 3 | time 单位前带空格 | `@ 0 ps` / `@ 6933.414503 us` | OpenTitan / 双时间精度仿真器 |
| 4 | OpenTitan 自定义 server | `UVM_FATAL @ 0 ps: (file.sv:161) [ral] msg` | `(file:line)` 移到 time 之后；无独立 reporter token |
| 5 | verbosity 前缀 | `UVM_ERROR(MEDIUM) ...` | `show_verbosity=1` |
| 6 | id 含空格 | `[ASSERT FAILED]` | 自定义 server 中常见 |
| 7 | 参数化 class id | `[uvm_driver #(REQ,RSP)]` | template class hierarchy（BUG-028 已修） |
| 8 | reporter 带 `@@context` | `agt.drv@@seq_id` | `context_str = "@@" + get_context()` |
| 9 | reporter 含数组索引 | `agt[0].drv[1]` | 数组化 component |
| 10 | filename 含宏未展开 | `$$STRING$$/path/file.sv(418)` | riscv-dv 等项目编译宏未展开 |
| 11 | id 为空 `[]` | 极罕见 | 规范不禁止 |

### 漏检根因（升级前正则）

| 漏检变体 | 卡在哪段正则 | 根因 |
|---|---|---|
| 2 | `\s+(\S+)\((\d+)\)` 强制 | file(line) 段写死，无可选机制 |
| 3 | `([\d.]+\s*\w+)` 强制单位 | 单位前没空格匹配（用户写法是 `\s*` 但 `\w+` 要求至少 1 字符，所以无单位时也 fail；且 `\s*\w+` 不允许"数字+空格+单位"） |
| 4 | 整体结构差异 | reporter 是必须的 |
| 5 | severity 后立即接空白 | 不支持 `(MEDIUM)` 后缀 |
| 11 | `[^\]]+` 的 `+` | 不允许空 id |

### 修复方案

[core/log_parser.py:9-26](log_analysis/triage_tool/core/log_parser.py#L9-L26) `_UVM_PATTERN` 整体改用命名组、按 IEEE 1800.2 模板把每个字段单独建模，缺失字段全部包成可选：

```python
_UVM_PATTERN = re.compile(
    r'(?P<level>UVM_(?:ERROR|WARNING|FATAL))'          # severity
    r'(?:\([^)]*\))?'                                  # 可选 verbosity 后缀 (MEDIUM)
    r'(?:\s+(?P<file>\S+)\((?P<line>\d+)\))?'          # 可选 file(line)
    r'\s+@\s*(?P<time>[\d.]+(?:\s*[a-z]+s)?)'          # time（数字 + 可选单位，单位前可有空格）
    r'\s*:\s*'
    r'(?:(?P<reporter>\S+)\s+)?'                       # 可选 reporter / hier_path
    r'\[(?P<id>[^\]]*)\]\s*'                           # id（允许空 + 空格 + 特殊字符）
    r'(?P<msg>.*)',                                    # 描述
    re.IGNORECASE
)
```

[core/log_parser.py:105-141](log_analysis/triage_tool/core/log_parser.py#L105-L141) 联动改用命名组访问，并对 `file` 为 None 时把 `location` 设为空字符串：

```python
_file     = m.group('file')
_line_no  = m.group('line')
_location = f"{_file}({_line_no})" if _file else ''
```

### 关键设计取舍

- **OpenTitan 变体 (#4) 不为它单独写第二条正则**：让 `(?:(?P<reporter>\S+)\s+)?` 的 `\S+` 贪婪把整段 `(file:line)` 吞进 reporter 槽。level/id/msg 仍正确提取，**不漏检**；只是 file/line 不再单独抽出（location 字段为空，但 reporter 字段保留 raw 文本，用户在 UI 上仍能看到来源）。
- **不引入双正则 fallback**：单条正则覆盖全部 11 种变体，可读性、可测试性、性能都最优。
- **命名组**：取代之前的 `m.group(1..7)`，避免新增 / 调整字段时索引漂移；代码内 group 访问也更自解释。

### 回归测试

[tests/test_log_parser.py](log_analysis/triage_tool/tests/test_log_parser.py) 在 `class TestParseLog` 内新增 8 条 `test_variant_*` 测试，每条覆盖一个独立变体（含用户两次报告的真实样本、OpenTitan 真实样本、riscv-dv 真实样本）。配合原有 `test_uvm_id_with_parametrized_type`（BUG-028）和 `test_uvm_error_extracted` 等基线测试，**24/24 通过**。

未来再发现新变体时：直接在测试里加一条 `test_variant_xxx`，验证现有正则是否覆盖；如不覆盖再决定是否扩展正则。

---

## BUG-028 含参数化 class 名的 UVM_ERROR 漏匹配：结果页 FAIL=1 但细分计数全 0

**发现日期**：2026-06-02
**状态**：已修复

### 现象

内网用户上传 `tc_016_txrx_ptr_full_666666.log`，结果页显示：

- 日志总数 1、PASS=0、**FAIL=1**
- UVM_FATAL / UVM_ERROR / UVM_WARNING **全为 0**
- 未匹配=0

但日志中确实存在一条 UVM_ERROR：

```
UVM_ERROR /share/project/.../com_driver.sv(283) @ 19249.00ns: uvm_test_top.env.m_com_pipe_pb_wr_agt[0].drv [uvm_driver #(REQ,RSP)] wait tr_cycle_num('d91) < tr_queue_num_max('d91) timeout.from:14248.69ns to:19249.20ns
```

画面呈现"FAIL 但 0 error"的矛盾状态，用户无法看到具体报错信息。

### 根因分析

`core/log_parser.py` 的 `_UVM_PATTERN` 用 `\[(\w+)\]` 匹配错误 ID 字段：

```python
r'\[(\w+)\]\s*(.*)',              # Group6: 错误ID, Group7: 描述
```

`\w` 等价于 `[A-Za-z0-9_]`，无法覆盖 UVM 参数化 class 名里出现的 **空格、`#`、`(`、`)`、`,`、`-`** 等字符。
`[uvm_driver #(REQ,RSP)]` 这种典型参数化类型名因此让整行正则匹配失败：

- 组件路径 `uvm_test_top.env.m_com_pipe_pb_wr_agt[0].drv` 被 `(\S+)` 贪婪整体吃下 ✓
- 中间 `\s+` 吃空格 ✓
- 到 `\[(\w+)\]` 时，`uvm_driver` 后面是空格——`\w` 不接受空格，整体回溯失败 ✗

漏匹配 → `statistics['UVM_ERROR']` 不增长 → `has_error=False`。用户配置了 `pass_patterns.json` 但日志没有 pass 标记 → 走 pass/fail 判定的兜底分支 `status='fail'`。这就是"FAIL=1 但 errors=0"的来源。

### 修复方案

放宽 ID 字段字符集，只排除右方括号：

```python
# 修复前
r'\[(\w+)\]\s*(.*)',              # Group6: 错误ID, Group7: 描述

# 修复后
r'\[([^\]]+)\]\s*(.*)',           # Group6: 错误ID（允许空格/#/(),- 等参数化字符）, Group7: 描述
```

**为什么 `[^\]]+` 是安全的**：
- UVM 规范下 `[ID]` 字段内部不会再嵌套 `]`，"非 `]` 一次或多次"等价于"括号内全部内容"。
- 字段仍以左右方括号为边界，按行匹配，不会跨越到下一段。
- 组件路径里的 `[0]` 数组索引由 `(\S+)` 贪婪先吃下，不进 ID 槽位，行为与现状一致。

### 触发场景

凡是组件实例化时携带 type parameter 的 UVM 类都会触发，例如：

- `[uvm_driver #(REQ,RSP)]`
- `[uvm_sequencer #(T)]`
- `[uvm_subscriber #(my_pkt)]`
- 自定义参数化 component / agent

### 回归测试

`tests/test_log_parser.py::TestParseLog::test_uvm_id_with_parametrized_type` 用用户提供的真实日志行作为样本，断言：

- `statistics['UVM_ERROR'] == 1`
- `top_errors[0]['error_id'] == 'uvm_driver #(REQ,RSP)'`（保留原 ID 串、含空格）
- `top_errors[0]['description']` 以 `'wait tr_cycle_num'` 开头
- `top_errors[0]['location']` 含 `com_driver.sv(283)`

---

## BUG-027 LLM 连接测试秒回"未返回内容"：reasoning model content 字段为 null

**发现日期**：2026-04-19
**状态**：已修复

### 现象

配置内网 LLM（`gpt-oss-120b`，gpustack 部署），点击连接测试后几乎立刻返回：

> 连接失败：LLM 未返回内容，请检查端点/密钥/模型名称

无具体错误信息，且响应极快（说明 HTTP 请求本身已到达服务器并收到 200）。

### 调试过程

1. 先用 curl 验证模型列表和鉴权：
   ```bash
   curl http://<host>/v1/models -H "Authorization: Bearer <key>"
   ```
2. 确认 key 有效后，直接 curl 对话接口查看原始响应：
   ```bash
   curl -X POST http://<host>/v1/chat/completions \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer <key>" \
     -d '{"model":"gpt-oss-120b","messages":[{"role":"user","content":"hi"}],"max_tokens":20}'
   ```
3. 发现响应中 `"content": null`，实际输出在 `"reasoning"` 字段——该模型是思考型（reasoning model），推理过程存于 `reasoning`，`content` 为空。

### 根因分析

`_parse_response`（`core/llm_client.py`）直接读取 `choices[0].message.content`，当该字段为 `null` 时 Python 得到 `None`，返回空字符串，调用方判定为未返回内容。代码未考虑 reasoning model 的 `reasoning` 兜底字段。

```python
# 修复前
if 'choices' in data:
    return data['choices'][0]['message']['content']  # null → None → ""
```

### 修复方案

```python
# 修复后：content 为 null 时回落到 reasoning 字段
if 'choices' in data:
    msg = data['choices'][0]['message']
    return msg.get('content') or msg.get('reasoning') or ''
```

### 顺带排查的前置问题

| 症状 | 原因 | 解决 |
|---|---|---|
| 连接测试 404 | endpoint 填了 `/v1`（缺少 `/chat/completions`）| 填完整路径 `http://host/v1/chat/completions` |
| 连接测试 401 | 内网服务有鉴权，curl 查模型列表也返回 401 | 向服务管理员获取 API Key 填入配置 |

---

## BUG-026 Testlist 空格对齐失效 + 批量 RUN_NUM 不生效

**发现日期**：2026-04-16
**状态**：已修复

### 现象

1. 导出的 Testlist 文件中列不对齐：`tc_sanity1` 紧连，`tc_perf_test` 行与其他行不对齐
2. 在批量设置行修改 RUN_NUM 为 3，下载的文件仍显示默认值 1

### 根因分析

**对齐问题**：生成函数 `_generateTestlistText()` 原使用 Tab 分隔，但浏览器预览字体非等宽，Tab 宽度不固定导致视觉不对齐；下载文件在部分编辑器（tab stop 非整数倍时）也会错位。

**RUN_NUM 不生效**：`<input type="number">` 绑定的是 `onchange`（失焦才触发），用户修改数值后直接点下载按钮，输入框焦点未离开，`_tlData` 未更新，仍为旧值。

### 修复方案

1. `_generateTestlistText()` 改为动态计算各列最大宽度，用空格补齐（固定2空格列间距），任何编辑器下对齐
2. `<input type="number">` 改为 `oninput`（实时触发）
3. `_copyTestlist()` 和 `_downloadTestlist()` 调用前增加 `_syncInputsToData()`，强制从当前 DOM 读值同步到 `_tlData`

---

## BUG-025 切换 Tab 后返回，进度日志丢失只见转圈

**发现日期**：2026-04-03
**状态**：已修复

### 现象

分析进行中切换到「查询知识库」等其他 Tab，再切回「上传文件」或「指定路径」Tab，`progressWrap` 进度区不见了，只能看到分析按钮上的 `btnSpinner` 还在转，进度日志全部消失。

### 根因分析

`switchMode()` 在切换到非分析 Tab 时会将 `progressWrap` 设为 `display:none`（正确行为，避免空进度区残留），但切回分析 Tab 时只恢复了 `analyzeBtnRow` 的显示，没有重新显示 `progressWrap`。

SSE 连接（EventSource）是网络层对象，隐藏 DOM 不会中断它，`progLogs` 内容持续被 `_updateProgressUI` 写入，日志并未丢失——只是容器 div 隐藏了。

```javascript
// 修复前：切回分析 Tab 时未恢复 progressWrap
if (mode !== 'upload' && mode !== 'path') {
  document.getElementById('progressWrap').style.display = 'none';
}
// 没有对应的 else 分支
```

### 修复方案

在 `switchMode` 增加 `else if` 分支：切回分析 Tab 时，若 `analyzeBtn` 仍处于 `disabled`（即分析进行中），将 `progressWrap` 恢复为可见。

```javascript
// 修复后
if (mode !== 'upload' && mode !== 'path') {
  document.getElementById('progressWrap').style.display = 'none';
} else if (document.getElementById('analyzeBtn').disabled) {
  document.getElementById('progressWrap').style.display = '';
}
```

**为何用 `analyzeBtn.disabled` 作为判断依据**：分析开始时按钮被 disable，分析结束（完成/失败/超时）时由 `_resetAnalyzeBtn()` 统一恢复 enable，与进度区生命周期完全对齐，无需额外状态变量。

### 涉及文件

- `templates/index.html`：`switchMode()` 函数（约第 268 行）

---

## BUG-017 _store 并发访问竞态条件导致去重详情页信息丢失

**发现日期**：2026-04-01
**状态**：已修复

### 现象

路径模式分析大量日志完成后，汇总栏去重报错数字（如 UVM_ERROR 6条）显示正常，但点击进入 `/errors?level=xxx` 详情页时列表为空。偶发，非必现。

### 根因分析

`_store` 字典无任何同步机制，后台分析线程（`_run_analysis`）调用 `_set_results` 写入时，Flask 请求线程可能同时调用 `_get_results` 执行读取 + TTL 过期清理（`del _store[k]`），导致两种竞态：

1. **写入与读取并发**：后台线程写入 `_store[sid]` 期间，请求线程读到部分写入的数据（Python dict 赋值为原子操作，但 TTL 清理的 `del` + `get` 组合不原子），导致 `entry` 为 `None`，返回空结果。
2. **TTL 清理误删**：后台线程写入后 `ts` 未及时更新，或清理窗口恰好卡在写入前，`del _store[k]` 将刚写入的 sid 误删。

`/result` 页面通常在后台线程完成后首次加载（数据已稳定），而 `/errors` 是二次点击，若恰好另一用户的会话触发了 TTL 清理，同一次 `_get_results` 调用中 `del _store[k]` 遍历时可能波及当前 sid。

### 修复方案

新增 `_store_lock = threading.Lock()`，在 `_get_results` 和 `_set_results` 中用 `with _store_lock` 保护所有对 `_store` 的读写和 TTL 清理操作。锁持有时间极短（内存操作），性能影响可忽略。

```python
# 修复前
def _get_results(sid):
    stale = [k for k, v in list(_store.items()) if ...]
    for k in stale: del _store[k]   # 无锁
    return ...

def _set_results(sid, results, db_path):
    _store[sid] = {...}              # 无锁

# 修复后
_store_lock = threading.Lock()

def _get_results(sid):
    with _store_lock:
        stale = [...]
        for k in stale: del _store[k]
        return ...

def _set_results(sid, results, db_path):
    with _store_lock:
        _store[sid] = {...}
```

### 涉及文件

- `app.py`（`_store_lock` 定义，`_get_results`，`_set_results`）

---

## BUG-014 Linux 路径模式多用例扫描：启动慢 + 完成后圆圈持续转动

**发现日期**：2026-03-29
**状态**：已修复

### 现象

在 Linux 系统下，使用"指定路径"模式扫描大量日志用例时出现两个问题：
1. 点击"开始分析"后，有明显等待（无进度条），才开始显示扫描进度
2. 分析完成后，页面不跳转，进度圈持续转动，用户卡在分析页

### 根因分析

**缺陷一：glob 展开阻塞请求处理线程（导致启动慢）**

`/analyze` 路由在返回 `job_id` 之前，同步执行 `glob.glob()` + `Path.is_file()` 遍历所有匹配文件。文件数量多时，这一步本身需要几十秒，期间前端只能看到按钮 Spinner，进度条无法显示，体验上形同卡死。

**缺陷二：Linux TCP close-before-delivery 导致 SSE 最终事件丢失（导致圆圈不停）**

SSE 生成器发出 `phase='done'` 数据后立即 `return`，服务端随即关闭 TCP 连接。Linux TCP 栈会将数据帧和 FIN 打包在同一或相邻帧发出。浏览器 EventSource 实现可能先处理 TCP 关闭事件（触发 `onerror`），再处理缓冲区中的数据（触发 `onmessage`）。`onerror` 先调 `es.close()`，之后 `onmessage` 不再触发，导致 `phase='done'` 的重定向逻辑永远不执行，圆圈持续转动。

### 修复方案

**缺陷一修复**：将 glob 展开移入后台线程。`/analyze` 路由仅做格式校验，立即创建 job（`phase='scanning'`）并返回 `job_id`。后台线程依次完成：扫描文件（scanning）→ 解析日志（parsing）→ 知识库匹配（matching）。前端新增 `scanning` 状态显示"正在扫描文件..."。

**缺陷二修复（双保险）**：
- 后端：SSE 生成器发出 done/error 事件后 `time.sleep(1)` 再关连接，给客户端留足处理时间
- 前端：`onerror` 不再直接 `_resetAnalyzeBtn()`，改为 fetch `/progress_status/<job_id>` 单次轮询真实状态；若状态为 done 则重定向，为 error 则显示错误，否则重置按钮

### 涉及文件

- `app.py`：`_run_analysis` 加 scanning 阶段；`/analyze` 路由不再 glob；SSE 生成器加 1s 延迟；新增 `/progress_status/<job_id>` 路由
- `templates/index.html`：`onerror` 改为 fallback 轮询；`_updateProgressUI` 增加 scanning 状态

---

## BUG-013 指定路径模式批量扫描完成后，按钮 Spinner 持续转动不停

**发现日期**：2026-03-26
**状态**：已修复

### 现象

使用"指定路径"模式批量扫描日志，分析完成后页面不跳转，"开始分析"按钮的加载动画（Spinner）持续转动，用户需手动刷新页面才能查看结果。

### 根因分析

存在两处独立缺陷叠加触发。

**缺陷一：后台线程竞态导致前端收到 `redirect: null`**

`_run_analysis` 后台线程在标记任务完成时，`phase` 与 `redirect` 赋值顺序有误：

```python
# 修复前（错误顺序）
job['phase']    = 'done'       # ← 先写 phase
job['redirect'] = '/result'   # ← 后写 redirect
```

SSE 生成器在 Flask 主线程中每 0.3 s 轮询 `_jobs`。若在两条赋值之间读到 job，会推送 `{"phase": "done", "redirect": null}`。前端收到后执行：

```javascript
setTimeout(() => { window.location.href = null; }, 600);
```

`window.location.href = null` 在部分浏览器中静默失败，页面留在原地，Spinner 永远不停。

**缺陷二：`es.onerror` 未重置按钮**

```javascript
// 修复前
es.onerror = function() { es.close(); };
```

SSE 连接因任何原因断开（包括服务端正常关闭流）时，浏览器触发 `onerror`，此处只关闭了 EventSource，未调用 `_resetAnalyzeBtn()`，Spinner 同样持续转动。

### 修复方案

**`app.py`**，交换赋值顺序，确保 `redirect` 在 `phase` 之前写入，消除竞态窗口：

```python
# 修复后（正确顺序）
job['redirect'] = '/result'   # 先写 redirect
job['phase']    = 'done'      # 再写 phase
```

**`templates/index.html`**，两处修改：

```javascript
// 修复一：不依赖 payload 中的 redirect 字段，改用硬编码路径
// 修复前
setTimeout(() => { window.location.href = d.redirect; }, 600);
// 修复后
setTimeout(() => { window.location.href = '/result'; }, 600);

// 修复二：onerror 补充重置按钮，防止连接异常时 Spinner 卡死
// 修复前
es.onerror = function() { es.close(); };
// 修复后
es.onerror = function() { es.close(); _resetAnalyzeBtn(); };
```

### 涉及文件

- `app.py`
- `templates/index.html`

---

## BUG-012 上传临时文件运行期间持续累积，磁盘空间无限增长

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

上传模式下，每次分析都将日志文件保存到 `uploads/` 目录，运行期间文件持续累积，长期不重启服务时磁盘空间无限增长。原有清理仅在**启动时执行一次**（删除24小时以上旧文件），运行中产生的新文件不受清理。

### 根因分析

`/analyze` 路由将上传文件保存到 `uploads/` 后，解析完成即可丢弃（结果已存入内存 `_store`），但原实现未在解析后删除临时文件，完全依赖重启触发清理。

### 修复方案

**`app.py`**，上传模式解析完成后立即删除临时文件，启动时清理作为兜底保留：

```python
# 上传模式：解析完成后立即删除临时文件（结果已存入内存，文件不再需要）
if not path_mode:
    for fp in saved_paths:
        try:
            Path(fp).unlink()
        except OSError:
            pass
```

### 涉及文件

- `app.py`

---

## BUG-011 `parse_log` 一次性加载整个文件，大文件导致内存溢出

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

解析 1GB 以上日志文件时，服务进程内存占用急剧上升（一个 10GB 文件约消耗 20GB 内存），并行解析多个大文件时内存压力成倍叠加，最终触发 OOM 或系统 Swap 导致严重卡顿。

### 根因分析

```python
# 修复前（一次性加载）
lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
```

`read_text()` 将整个文件读入一个字符串，`splitlines()` 再生成一份完整行列表，峰值内存约为文件大小的2倍。

### 修复方案

**`core/log_parser.py`**，改为逐行流式读取，用 `pending` 状态机处理续行逻辑（替代原来的下标前向查找）：

```python
# 修复后（流式读取，内存与文件大小无关）
with open(str(path), encoding='utf-8', errors='replace') as f:
    for raw_line in f:
        line = raw_line.rstrip('\n')
        # pending 状态机：遇到续行则缓冲，遇到终止条件则提交
        if pending is not None:
            if (stripped and not _UVM_ANY.search(stripped)
                    and line.startswith(' ') and len(cont_lines) < 3):
                cont_lines.append(stripped)
                continue
            # 续行终止，提交 pending
            ...
        m = _UVM_PATTERN.search(line)
        ...
```

任意大小文件解析期间内存占用仅为常数级（当前续行缓冲最多3行）。`top_errors` 满5条后仍继续扫描全文以统计准确的 FATAL/ERROR/WARNING 总数。

### 涉及文件

- `core/log_parser.py`

---

## BUG-010 `send_file(attachment_filename=...)` 在 Flask 2.0 下报 TypeError

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

在 Flask 2.0+ 环境下，导出 Excel / HTML 报告时抛出 `TypeError: send_file() got an unexpected keyword argument 'attachment_filename'`，报告下载功能完全不可用。

### 根因分析

Flask 2.0 将 `send_file` 的 `attachment_filename` 参数重命名为 `download_name`，旧参数名已被移除。

### 修复方案

**`app.py`**，`export_excel` 和 `export_html` 路由均改为 `download_name`：

```python
# 修复前
return send_file(out_path, as_attachment=True, attachment_filename=fname)

# 修复后
return send_file(out_path, as_attachment=True, download_name=fname)
```

### 涉及文件

- `app.py`

---

## BUG-009 Windows 下强制删除被持有的僵尸锁文件引发 PermissionError

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

在 Windows 环境中，当 `_FileLock` 检测到超时僵尸锁并调用 `os.remove()` 删除时，若对应进程仍持有文件句柄（但崩溃前未关闭），抛出 `PermissionError`，使整个锁等待循环崩溃，知识库写入失败。

### 根因分析

Linux 下文件删除是解除目录项引用，持有文件句柄的进程仍可访问直至句柄关闭。Windows 下文件被任意进程持有时，`os.remove()` 直接返回 `PermissionError`，行为与 Linux 不同。

### 修复方案

**`core/db_manager.py`**，将 `os.remove()` 包裹在 `try/except OSError`，Windows 无法删除时静默跳过，继续等待自然释放：

```python
# 修复前
os.remove(self.lock_path)

# 修复后
try:
    os.remove(self.lock_path)
except OSError:
    pass  # Windows: 文件仍被持有，等待自然释放
```

### 涉及文件

- `core/db_manager.py`

---

## BUG-008 `/writeback` 接口缺乏服务端输入校验，存在注入风险

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

`/writeback` 接口直接将前端 JSON 字段写入知识库，未校验 `level` 合法性，未校验 `reason` 非空，字段长度也无限制，可写入任意内容或超长字段。

### 根因分析

接口信任前端传入数据，未做任何服务端校验。

### 修复方案

**`app.py`**，`writeback` 路由增加三类校验：

```python
VALID_LEVELS = {'UVM_FATAL', 'UVM_ERROR', 'UVM_WARNING'}
MAX_LEN = 500

level = data.get('level', '').strip().upper()
if level not in VALID_LEVELS:
    return jsonify({'success': False, 'error': '无效的错误级别'}), 400
reason = data.get('reason', '').strip()
if not reason:
    return jsonify({'success': False, 'error': '报错原因不能为空'}), 400

# 所有字段截断至 500 字符
entry = {
    '错误类型': level,
    '报错原因': reason[:MAX_LEN],
    # ... 其余字段同样 [:MAX_LEN]
}
```

### 涉及文件

- `app.py`

---

## BUG-007 HTML 报告中动态内容未转义，存在 XSS 风险

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

生成的 HTML 报告使用字符串拼接直接插入文件名、错误描述、根因等字段，若日志中含 `<script>` 等 HTML 特殊字符，打开报告时会执行任意脚本。

### 根因分析

`reporter.py` 生成 HTML 时未对动态内容做 HTML 实体转义。

### 修复方案

**`core/reporter.py`**，引入 `html.escape` 并对所有动态字段应用：

```python
from html import escape as h

# 修复前
f'<td>{r["file"]}</td>'
f'<td>{err.get("description","")}</td>'

# 修复后
f'<td>{h(r["file"])}</td>'
f'<td>{h(err.get("description",""))}</td>'
```

### 涉及文件

- `core/reporter.py`

---

## BUG-006 Flask `secret_key` 硬编码导致 Session 可伪造

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

`app.py` 中 `secret_key` 为硬编码固定字符串，攻击者知晓后可伪造任意 Session Cookie，冒充其他用户读取分析结果。

### 根因分析

固定 `secret_key` 等同于无 Cookie 签名保护。

### 修复方案

**`app.py`**，首次启动时用 `secrets.token_bytes(32)` 生成随机密钥并持久化到 `.secret_key` 文件；Linux 下 `chmod 0o600` 限制读取权限：

```python
_key_file = BASE_DIR / '.secret_key'
if _key_file.exists():
    app.secret_key = _key_file.read_bytes()
else:
    _key = secrets.token_bytes(32)
    _key_file.write_bytes(_key)
    if sys.platform != 'win32':
        os.chmod(str(_key_file), 0o600)
    app.secret_key = _key
```

### 涉及文件

- `app.py`

---

## BUG-005 上传文件名未净化，存在路径穿越漏洞

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

上传模式下，构造含 `../` 的文件名（如 `../../etc/passwd.log`）可将文件写到 `uploads/` 目录之外的任意位置。

### 根因分析

`app.py` 直接使用 `f.filename` 拼接保存路径，未做任何净化。

### 修复方案

**`app.py`**，使用 `werkzeug.utils.secure_filename` 净化文件名；空白/纯非法字符文件名回退到随机名：

```python
from werkzeug.utils import secure_filename

safe_name = secure_filename(f.filename) or f'file_{uuid.uuid4().hex[:8]}.log'
save_path = UPLOAD_DIR / f'{sid}_{safe_name}'
```

### 涉及文件

- `app.py`

---

## BUG-004 错误描述多行续行仅拼接1行，描述信息截断

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

UVM 错误描述跨越多行时，解析结果仅包含首行内容，后续关键信息（如具体断言值、堆栈第二行）丢失，导致知识库关键词匹配率下降。

### 根因分析

`log_parser.py` 原实现只向后读取1行续行，且判断条件为"下一行不是 UVM 条目"，未过滤空行，遇到空行后续内容也会被误拼接。

### 修复方案

**`core/log_parser.py`**，最多续读3行，遇到 UVM 条目行、空行或非缩进行停止：

```python
_UVM_ANY = re.compile(r'UVM_(?:ERROR|WARNING|FATAL|INFO)\s', re.IGNORECASE)

extra = []
for j in range(i + 1, min(i + 4, len(lines))):
    next_line = lines[j].strip()
    if not next_line or _UVM_ANY.search(next_line) or not lines[j].startswith(' '):
        break
    extra.append(next_line)
if extra:
    description = description + ' ' + ' '.join(extra)
```

### 涉及文件

- `core/log_parser.py`

---

## BUG-003 `_FileLock` 僵尸锁年龄用 `time.monotonic()` 与 mtime 比较导致判断错误

**发现日期**：2026-03-12
**状态**：已修复
**版本**：v1.2

### 现象

`_FileLock` 超时检测逻辑中，用于判断僵尸锁年龄的时间基准与文件 mtime 时间基准不一致，导致僵尸锁可能永远不被清理，或正常锁被误判为僵尸锁。

### 根因分析

```python
# 修复前（错误）
age = time.monotonic() - os.path.getmtime(self.lock_path)
```

`time.monotonic()` 返回系统单调时钟（相对于系统启动的秒数），`os.path.getmtime()` 返回 Unix 时间戳（1970年起的秒数）。两者相减没有物理意义，数值差异巨大（后者通常大 17 亿），导致 `age > STALE_TIMEOUT(60)` 永远为真，每个锁文件创建后立即被误判为僵尸锁删除。

### 修复方案

**`core/db_manager.py`**，改用 `time.time()` 使两侧时基一致：

```python
# 修复后（正确）
age = time.time() - os.path.getmtime(self.lock_path)
```

### 涉及文件

- `core/db_manager.py`

---

## BUG-002 删除文件后重新选择同一文件无法加载

**发现日期**：2026-03-08
**状态**：已修复
**版本**：v1.2

### 现象

首次选择 `sim.log` 并加载成功，点击 ✕ 从列表移除后，再次选择同一个 `sim.log`，文件列表无响应，`change` 事件不触发。

### 根因分析

浏览器对 `<input type="file">` 的 `change` 事件触发条件是：**input 的值发生变化**。
首次选择后 input 内部记录了文件路径；即便从 JS 的 `selectedFiles` 数组中移除了该文件条目，input 本身的值未被清除。再次选择相同文件时，浏览器判断路径未变化，不触发 `change` 事件。

### 修复方案

**`templates/index.html`**，在 `change` 回调中处理完文件后立即重置 input 值：

```javascript
// 修复前
fileInput.addEventListener('change', () => addFiles(fileInput.files));

// 修复后
fileInput.addEventListener('change', () => {
    addFiles(fileInput.files);
    fileInput.value = '';   // 清空，确保再次选同一文件时仍触发 change 事件
});
```

### 注意

模板文件由 Flask 每次请求动态读取，此修复**无需重新打包 exe**，重启 exe 后生效。

### 涉及文件

- `templates/index.html`

---

## BUG-001 文件选择后未加载，点击分析提示"请先选择日志文件"

**发现日期**：2026-03-08
**状态**：已修复
**版本**：v1.1 → v1.2

### 现象

在首页点击"点击选择文件"，选择仿真日志文件后，文件列表区域无任何显示；点击"开始分析"按钮提示"请先选择日志文件"。

### 根因分析

两个独立原因叠加：

**原因1：`accept` 属性过滤导致 `change` 事件不触发**

```html
<!-- 修复前 -->
<input type="file" id="fileInput" multiple accept=".log,.txt" style="display:none">
```

浏览器文件对话框设置了 `accept=".log,.txt"` 过滤器。当用户选择的文件扩展名不在此列表中（如 `.out`、`.rpt`、无扩展名等常见 EDA 仿真日志格式）时，部分浏览器（Chrome/Edge）在关闭对话框后**静默丢弃**选择结果，不触发 `change` 事件，导致 `selectedFiles` 数组始终为空。

服务端解析器（`core/log_parser.py`）本身按文本内容匹配 UVM 正则，不依赖文件扩展名，故前端 `accept` 限制是多余的约束。

**原因2：拖拽区可点击区域过小**

```html
<!-- 修复前 -->
<div class="drop-zone" id="dropZone">
  ...
  <label for="fileInput" class="link-btn">点击选择文件</label>
```

整个拖拽框只有 `<label>` 文字部分响应点击，点击图标或空白区域无反应，用户误操作概率高。

### 修复方案

**`templates/index.html`**：

```html
<!-- 修复后 -->
<div class="drop-zone" id="dropZone" onclick="fileInput.click()">
  <div class="drop-icon">📂</div>
  <div class="drop-text">拖拽日志文件到此处，或 <span class="link-btn">点击选择文件</span></div>
  <div class="drop-hint">支持任意文本日志格式，可多选</div>
  <input type="file" id="fileInput" multiple style="display:none">
</div>
```

- `accept` 改为仅 `.log`，在 JS `addFiles()` 中增加扩展名二次校验，非 .log 文件给出提示并跳过（双重过滤，不依赖浏览器静默丢弃）
- 将 `onclick="fileInput.click()"` 移至 `dropZone` div，整个拖拽区域均可点击触发文件选择
- `<label for="fileInput">` 改为普通 `<span>`，避免与 div 的 onclick 重复触发

### 验证方法

1. 选择 `.log` 文件 → 应正常显示在文件列表
2. 选择无扩展名或 `.out`、`.rpt` 等文件 → 应正常显示在文件列表
3. 点击拖拽区图标和空白处 → 应弹出文件选择对话框
4. 完整流程：选文件 → 开始分析 → 正常跳转结果页

### 涉及文件

- `templates/index.html`（逻辑修改）
- `dist/triage_tool.exe`（重新打包）

---

## BUG-015 切换 Tab 后进度区（"分析完成，正在跳转"）残留

**发现日期**：2026-03-30
**状态**：已修复

### 现象

分析完成后进度区显示"分析完成，正在跳转"日志。此时切换到「查询知识库」「添加条目」「解析配置」任意 Tab，进度区依然可见，干扰操作。

### 根因分析

`switchMode()` 函数切换 Tab 时只负责显示/隐藏各模式 div 和按钮行，未对 `progressWrap` 做任何处理。进度区是独立的全局 DOM 节点，不属于任何模式 div，因此切 Tab 不会隐藏它。

### 修复方案

在 `switchMode()` 末尾追加：

```javascript
if (mode !== 'upload' && mode !== 'path') {
  document.getElementById('progressWrap').style.display = 'none';
}
```

切换到非分析 Tab 时立即隐藏进度区；切回「上传文件/指定路径」Tab 时保持原有状态（进度区仍可见，符合预期）。

### 涉及文件

- `templates/index.html`（`switchMode` 函数新增3行）
- `dist/triage_tool.exe`（重新打包）

---

## BUG-024 SSE 进度流 JSON 解析错误被静默吞掉 + 服务端挂死时按钮永久禁用

**发现日期**：2026-04-02
**状态**：已修复

### 现象

1. SSE 进度流收到非法 JSON 数据时，`catch(_) {}` 静默丢弃，界面无任何反馈，进度条卡住
2. 服务端进程挂死（非正常退出）时，`EventSource` 持续重连，"开始分析"按钮永远禁用，用户只能强制刷新页面

### 根因分析

```javascript
// 修复前
try {
    const d = JSON.parse(event.data);
    ...
} catch(_) {}   // ← 吞掉所有错误
```

`catch` 块为空，JSON 解析失败后流程中断但无任何提示。`EventSource` 本身没有超时机制，`onerror` 仅在 TCP 断开时触发；服务端挂死时 TCP 连接保持，`onerror` 不触发，按钮永久禁用。

### 修复方案

**`templates/index.html`**：

1. 将 `catch(_) {}` 改为 `catch(parseErr) { console.error('[SSE] JSON 解析失败:', parseErr, event.data); }`
2. 添加 30 秒无活动超时计时器（`_resetTimeout`），每收到一条消息重置，超时后关闭连接并提示用户
3. `onerror` 处理器中调用 `clearTimeout(_timeout)` 防止泄漏

```javascript
let _timeout = setTimeout(function() {
    es.close();
    document.getElementById('errMsg').textContent = '分析超时（30秒无响应），请检查服务端状态后重试';
    _resetAnalyzeBtn();
}, 30000);
function _resetTimeout() { clearTimeout(_timeout); _timeout = setTimeout(..., 30000); }
es.onmessage = function(event) {
    _resetTimeout();
    try { ... } catch(parseErr) { console.error('[SSE] JSON 解析失败:', parseErr, event.data); }
};
es.onerror = function() { clearTimeout(_timeout); es.close(); fetch('/progress_status/...') ... };
```

### 涉及文件

- `templates/index.html`（`_connectProgressStream` 函数）

---

## BUG-023 `_get_results()` 直接访问字典字段，结构不完整时抛 KeyError

**发现日期**：2026-04-02
**状态**：已修复

### 现象

`state._store[sid]` 结构不完整时（如存在旧格式条目或手动注入测试数据），`_get_results()` 抛 `KeyError`，导致 `/result`、`/errors`、`/export` 等多个路由返回 HTTP 500。

### 根因分析

```python
# 修复前
entry = _store.get(sid)
return (entry['results'], entry['db_path']) if entry else ([], DB_DEFAULT)
```

`entry` 非 None 但缺少 `results` 或 `db_path` 字段时，直接访问抛 `KeyError`。

### 修复方案

**`state.py`**，改为 `.get()` 并提供默认值：

```python
# 修复后
entry = _store.get(sid)
return (entry.get('results', []), entry.get('db_path', DB_DEFAULT)) if entry else ([], DB_DEFAULT)
```

### 涉及文件

- `state.py`（`_get_results` 函数）

---

## BUG-022 `_jobs` 字典并发清理无锁保护，存在竞态条件

**发现日期**：2026-04-02
**状态**：已修复

### 现象

SSE 端点 `/progress/<job_id>` 清理过期 `_jobs` 条目时无锁保护，与后台分析线程写入 `_jobs[job_id]` 并发，可能导致：
1. `dict changed size during iteration` 异常
2. 正在运行的任务被误删，前端收到"任务不存在"错误
3. 过期条目不清理导致内存持续增长（若 SSE 端点从未被调用）

### 根因分析

```python
# 修复前（无锁）
stale = [k for k, v in list(state._jobs.items())
         if now - v.get('ts', 0) > state._JOBS_TTL]
for k in stale:
    del state._jobs[k]
```

对比：`_store` 的清理在 `_store_lock` 保护下执行，`_jobs` 清理无任何同步保护，不一致。

### 修复方案

**`state.py`**，新增 `_jobs_lock = threading.Lock()` 和 `_cleanup_jobs()` 函数：

```python
_jobs_lock = threading.Lock()

def _cleanup_jobs():
    with _jobs_lock:
        now   = time.time()
        stale = [k for k, v in list(_jobs.items()) if now - v.get('ts', 0) > _JOBS_TTL]
        for k in stale:
            del _jobs[k]
```

**`blueprints/analysis.py`**：
- SSE 生成器中改为调用 `state._cleanup_jobs()`
- `/analyze` 路由在创建新任务前调用 `state._cleanup_jobs()`，并以 `with state._jobs_lock:` 保护任务字典写入

### 涉及文件

- `state.py`（新增 `_jobs_lock`、`_cleanup_jobs`）
- `blueprints/analysis.py`（`progress_stream`、`analyze`）

---

## BUG-021 `update_entry`/`delete_entry` 无异常处理，知识库文件损坏时返回 HTTP 500

**发现日期**：2026-04-02
**状态**：已修复

### 现象

知识库 Excel 文件损坏（如写入中途崩溃）或被独占时，`update_entry()` 和 `delete_entry()` 的 `openpyxl.load_workbook()` 抛出未捕获异常，穿透到 Flask 路由层，返回无任何错误提示的 HTTP 500。

### 根因分析

```python
# 修复前（无异常处理）
with _thread_lock:
    with _FileLock(db_path):
        wb = openpyxl.load_workbook(db_path)   # ← 损坏文件直接抛异常
        ...
```

### 修复方案

**`core/db_manager.py`**，在两个函数中包裹 try/except，抛出语义化 `ValueError`：

```python
# 修复后
try:
    wb = openpyxl.load_workbook(db_path)
except Exception as e:
    raise ValueError(f'无法读取知识库文件（可能已损坏或被独占）: {e}')
```

调用方（`blueprints/kb.py`）已有 `try/except Exception` 包裹，会将 `ValueError` 转为带 message 的 JSON 错误响应。

### 涉及文件

- `core/db_manager.py`（`update_entry`、`delete_entry`）

---

## BUG-020 `load_db()` 未做行列数边界检查，用户手动编辑 Excel 缺列时抛 IndexError

**发现日期**：2026-04-02
**状态**：已修复

### 现象

用户手动编辑 `error_db.xlsx` 时误删列，导致某些数据行的实际列数少于表头列数。`load_db()` 重试 3 次后向上抛出 `RuntimeError`，知识库读取功能完全不可用。

### 根因分析

```python
# 修复前（无边界检查）
entry = {headers[i]: (row[i] if row[i] is not None else '')
         for i in range(len(headers))}
# 若 len(row) < len(headers)，row[i] 抛 IndexError
```

### 修复方案

**`core/db_manager.py`**，加入列数边界检查：

```python
# 修复后
entry = {headers[i]: (row[i] if i < len(row) and row[i] is not None else '')
         for i in range(len(headers))}
```

缺失列自动填充空字符串，知识库仍可正常读取，不影响正常条目。

### 涉及文件

- `core/db_manager.py`（`load_db` 函数）

---

## BUG-019 `_sort_by_date()` 空日期排序方向与注释相悖，无日期条目排在最前

**发现日期**：2026-04-02
**状态**：已修复

### 现象

`_sort_by_date()` 用于对知识库命中条目按录入日期降序排列，注释明确"缺失日期排最后"。实际行为相反：无录入日期的条目排在最前，成为首选展示给用户的匹配结果。

### 根因分析

```python
# 修复前
key=lambda e: str(e.get('录入日期', '') or '')
```

`''`（空字符串）的字典序小于任何日期字符串（如 `'2024-01-01'`），`reverse=True` 降序排列后，空字符串反而排在最前。

### 修复方案

**`core/matcher.py`**，将空值替换为最小日期占位符：

```python
# 修复后
key=lambda e: str(e.get('录入日期', '') or '0000-00-00')
```

`'0000-00-00'` 字典序小于任何合法日期，`reverse=True` 后确保无日期条目排在最后。

### 涉及文件

- `core/matcher.py`（`_sort_by_date` 函数）

---

## BUG-018 `parse_logs()` 单文件解析失败导致整批任务中断

**发现日期**：2026-04-02
**状态**：已修复

### 现象

批量分析 1000 个日志文件时，若其中一个文件权限不足、已被删除或路径断开，`parse_logs()` 立即抛出未捕获异常，终止整个批次，其余所有文件的解析结果全部丢失。

### 根因分析

```python
# 修复前
for future in as_completed(future_to):
    i, fp = future_to[future]
    r = future.result()   # ← parse_log() 内部异常穿透至此，整批中断
    results[i] = r
```

`parse_log()` 内部的 `open()` 调用无 try/except，权限不足、文件消失等均会抛出未捕获异常。

### 修复方案

**`core/log_parser.py`**，新增 `_error_result()` 占位符函数，`parse_logs()` 捕获单文件异常后返回占位结果：

```python
def _error_result(filepath: str, error_msg: str) -> dict:
    return {
        'file': Path(filepath).name, 'filepath': str(filepath),
        'statistics': {'UVM_WARNING': 0, 'UVM_ERROR': 0, 'UVM_FATAL': 0},
        'status': 'fail', 'pass_found': False,
        'top_errors': [], 'all_errors': [],
        'error': error_msg,
    }

# parse_logs() 中
try:
    r = future.result()
except Exception as e:
    r = _error_result(fp, str(e))
results[i] = r
```

调用方可通过检查 `result.get('error')` 字段识别失败条目，其余文件正常返回。

### 涉及文件

- `core/log_parser.py`（`_error_result` 新增，`parse_logs` 修改）

---

## BUG-016 打开「解析配置」Tab 后页面卡死（无限循环）+ 额外关键词含空格导致 ID 非法

**发现日期**：2026-03-30
**状态**：已修复

### 现象

1. 在 Windows（或任何平台）打开「解析配置」Tab 后，页面长时间无响应（浏览器弹出"页面无响应"对话框）
2. 含空格的默认关键词（如 `JVP TEST FAILED`）在关键词配置列表中无法正常编辑/删除

### 根因分析

**Bug 1（无限循环，主因）**

`_populateLevelSelects()` 中：
```javascript
while (dl.options.length > 3) dl.remove(3);
```
`dl` 是 `<datalist>` 元素。`<datalist>` 没有 `remove(index)` 方法；调用 `dl.remove(3)` 实际调用了继承自 `HTMLElement` 的 `remove()` 方法（无参数，忽略传入的 `3`），将整个 datalist 元素从 DOM 中移除。但 `dl.options.length` 在 datalist 脱离 DOM 后不会减少（内存中选项仍存在），导致 `while` 条件永远为真，进入**无限循环**。

触发时机：页面加载时 fetch `/extra_patterns` 会调用 `_populateLevelSelects`，此时 `dl.options.length=3`（初始），loop 不进入，正常追加若干 options（length 变为 3+N）。之后用户打开「解析配置」Tab 触发 `kwRender`，再次调用 `_populateLevelSelects`，此时 `dl.options.length > 3`，进入死循环。

**Bug 2（非法 HTML ID）**

`kwRender` 使用 `kw` 直接拼接元素 ID：`id="kwrow-JVP TEST FAILED"`。HTML ID 不允许包含空格；`document.getElementById('kwrow-JVP TEST FAILED')` 在各浏览器行为不一致，导致编辑/删除按钮失效。此外，后端验证正则 `^[A-Z0-9_]+$` 拒绝含空格的关键词（而默认值中就含 `JVP TEST FAILED`），造成验证规则与实际数据不一致。

### 修复方案

**Bug 1**：将 `dl.remove(3)` 改为 `dl.options[3].remove()`，移除的是第 4 个 `<option>` 子元素而非 datalist 本身。

**Bug 2**：
- `kwRender` 改用数组下标（`i`）作为元素 ID（`id="kwrow-0"`, `id="kwrow-1"` 等），避免关键词内容出现在 ID 中；引入模块变量 `_kwPatterns` 存储当前列表，供 `kwStartEdit/kwCancelEdit/kwSaveEdit/kwDelete` 通过下标取回原始关键词值
- `app.py` 后端验证改为 `^[A-Z0-9_ ]+$`，允许关键词中包含空格（支持 `JVP TEST FAILED` 等多词关键词）

### 涉及文件

- `templates/index.html`（`_populateLevelSelects`、`kwRender`、`kwStartEdit`、`kwCancelEdit`、`kwSaveEdit`、`kwDelete`）
- `app.py`（`extra_patterns_add`、`extra_patterns_update` 验证正则和错误文字）
- `dist/triage_tool.exe`（重新打包）
