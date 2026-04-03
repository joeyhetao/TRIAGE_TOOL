# triage_tool — LLM 集成方案 v3.2

> **声明**：本方案为功能分析和架构设计，不含代码实现。
> **版本说明**：v3.2 在 v3.1 基础上为 P3 新增 Token 预算安全检查，解决小上下文模型（如 8K/32K）下内容超限导致 API 报错的问题；新增 `context_window` 配置字段，发送前自动收缩片段至模型可接受范围，不改变"不跳行"原则。

---

## Context

triage_tool（v2.2）是一个基于规则的 UVM 仿真日志分类分诊工具，核心能力已完善：

- 支持 UVM_FATAL/ERROR/WARNING + 额外关键词（extra_patterns）解析
- 知识库精确 ID 匹配 + 关键词 AND 匹配，两阶段命中
- 批量并行解析（ThreadPoolExecutor），PASS/FAIL 判断，去重统计
- 知识库增删改查、滚动备份、撤销删除
- 结果页完整展示、Excel/HTML 导出

工程师在使用中仍面临以下痛点：

1. **规则匹配盲区**：仅支持 error_id 精确匹配 + 关键词 AND 匹配，无法识别语义相似但表述不同的错误
2. **手工填写耗时**：每条未匹配错误需手动填写 5 个字段（根因分类、解决方案等），平均 2~5 分钟/条
3. **批量分析困难**：回归测试数十个 log 失败时，需人工归纳主要失败模式
4. **知识库维护成本高**：语义相似的重复条目难以及时发现和清理

**目标**：设计一套可选的 LLM 增强层，满足：
- 未配置 LLM 时，工具行为与现在 100% 一致（基础版）
- 配置 LLM 后，启用 AI 辅助功能（高配版）
- 不引入新的第三方依赖包（复用 `requests`，内网 Flask 环境已确认存在）
- 支持任意 OpenAI-compatible 接口

---

## 一、版本差异对比

| 功能模块 | 基础版（无 LLM） | 高配版（含 LLM） |
|---------|-------------------|-------------------|
| **未匹配错误** | 手动填写回写表单 | P1：一键 AI 分析 + 自动预填 |
| **多条匹配** | 按录入日期降序展示 | P2：LLM 按相关性重排 + 推荐理由 + 重点关注用例列表 |
| **自定义提取** | 不支持 | P3：自然语言查询 + 行号范围提取（Path 模式单文件） |
| **相似错误** | 无 | P4：语义相似 KB 条目推荐，辅助写回 |
| **批量分析** | 人工扫描统计 | P5：AI 自动归纳 3~7 个失败模式 |
| **知识库查询** | 关键词/ID 模糊匹配 | P6：语义搜索（规则预筛选 + LLM 重排，一键操作） |
| **知识库维护** | 基于字符串规则去重 | P7：AI 语义重复检测（滑动窗口 / 全量分批） |

---

## 二、核心架构设计

### 2.1 当前架构说明（v2.2 已实现）

```
log_analysis/triage_tool/
├── app.py                  # Flask 入口，仅注册 Blueprint + 初始化
├── state.py                # 所有共享状态（_store、_jobs、EXTRA_PATTERNS 等）
├── blueprints/
│   ├── analysis.py         # /analyze、/progress、/result 等
│   ├── writeback.py        # /writeback
│   ├── kb.py               # /kb/* 知识库管理
│   ├── config_bp.py        # /extra_patterns/*、/pass_patterns/*
│   └── export.py           # /export/*
├── core/
│   ├── log_parser.py       # 流式解析
│   ├── matcher.py          # 两阶段匹配
│   ├── db_manager.py       # Excel 读写 + 锁 + 备份
│   └── reporter.py         # 报告生成
└── templates/ static/
```

**`state.py` 中已预留的 LLM 字段**（v2.1 已实现，无需再改）：

```python
# _store[sid] 结构（已包含 LLM 字段）
{
    'results':    [...],
    'db_path':    str,
    'file_paths': [...],     # Path 模式文件路径列表（P3 需要）
    'p3_history': [...],     # P3 多轮对话消息历史
    'p3_tokens':  int,       # P3 累计 token 计数
    'ts':         float,
}
# _set_results() 已使用合并写入，不覆盖 p3_history/p3_tokens
```

### 2.2 LLM 新增模块

```
core/
  llm_client.py     ← 【新增】LLM API 客户端（无 Flask 依赖，约 120 行）
blueprints/
  llm_bp.py         ← 【新增】Flask Blueprint，9 条 LLM 路由（约 350 行）
```

> **注意**：新建 Blueprint 统一放在 `blueprints/` 目录（与现有 5 个 Blueprint 保持一致），文件名为 `llm_bp.py`，Blueprint 对象命名为 `llm_bp`。不再需要 `core/session_store.py`（旧方案遗留设计），`state.py` 已承担该职责。

### 2.3 app.py 改动点（3 处最小改动）

```python
# 1. 导入并初始化 LLM 客户端（在 BASE_DIR 确定后）
from core import llm_client
llm_client.init(BASE_DIR)
app.jinja_env.globals['llm_enabled'] = llm_client.is_configured()

# 2. 注册 LLM Blueprint
from blueprints.llm_bp import llm_bp
app.register_blueprint(llm_bp)

# 3. 无需改动 _store/file_paths（state.py 已完成，_set_results 已合并写入）
```

### 2.4 `core/llm_client.py` 接口

```
init(base_dir: Path) → None          # 加载 llm_config.json，环境变量覆盖
is_configured() → bool               # endpoint + model 均非空则 True
call_llm(prompt, system, temperature, max_tokens) → str  # 含超时重试，失败返回 ""
call_llm_with_cache(prompt, ...) → str   # 内存缓存包装（md5 key，TTL 可配置）
reload_config() → bool               # 热重载 llm_config.json，返回是否已配置
get_config() → dict | None           # 当前配置（用于 P3 采样计算）
```

**缓存**：模块级 `_cache dict`，key = `md5(prompt)[:16]`，`cache_ttl=0` 禁用，重启清空。
**重试**：指数退避，`llm_retry_delay × 2^attempt`，最多 `llm_max_retries` 次，最终失败返回 `""`。

### 2.5 `blueprints/llm_bp.py` 依赖关系

```python
import state                            # _sid(), _get_results(), _validate_db_path()
from core import llm_client             # call_llm / call_llm_with_cache
from core.db_manager import load_db     # P4 / P6 / P7 知识库读取
from core.matcher import score_query    # P4 候选预筛选（需新增此函数）
```

无循环依赖：`blueprints/llm_bp.py` → `state.py` / `core/*`，`app.py` → `blueprints/llm_bp.py`。

### 2.6 配置文件 `llm_config.json`

位置：`BASE_DIR/llm_config.json`（与 exe 同目录，不打包进 exe）。

```json
{
  "endpoint":              "http://your-llm-server/v1/chat/completions",
  "api_key":               "sk-xxx",
  "model":                 "qwen2.5-7b",
  "timeout":               30,
  "context_window":        100000,
  "p3_max_lines":          2500,
  "p3_chars_per_token":    4,
  "cache_ttl":             3600,
  "llm_max_retries":       2,
  "llm_retry_delay":       1,
  "kb_review_mode":        "fast",
  "kb_review_window_size": 20,
  "kb_review_step_size":   10,
  "kb_review_batch_size":  50
}
```

**P3 相关字段说明**：

| 字段 | 说明 | 典型值 |
|---|---|---|
| `context_window` | 模型实际支持的最大 token 数，用于 token 预算安全检查。默认 100K，对 128K/1M 模型开箱即用；使用 1M 模型时可改为 1000000 以发挥全部上下文 | 100000（默认）/ 128000（128K模型）/ 1000000（1M模型）|
| `p3_max_lines` | 预扫描滑动窗口大小（行数上限），决定"找最密集的多少行" | 2500（128K）/ 20000（1M）/ 200（8K）|
| `p3_chars_per_token` | 字符数/token 估算比例，用于 token 预算计算 | 4（保守值，适用中英混合日志）|

> `p3_max_lines` 是预扫描的搜索粒度，`context_window` 是最终发送的硬上限。通常只需按模型配置 `context_window`，`p3_max_lines` 保持默认即可（安全检查会自动收缩）。两者都设置时，取更严格的那个。

**环境变量覆盖（优先级高于文件）**：

| 环境变量 | 对应字段 |
|---|---|
| `LLM_ENDPOINT` | endpoint |
| `LLM_API_KEY` | api_key |
| `LLM_MODEL` | model |
| `LLM_TIMEOUT` | timeout（整数秒） |

### 2.7 `core/matcher.py` 新增函数

将 `blueprints/kb.py` 中 `/query` 路由内联的 token 重叠打分逻辑提取为独立函数，供 P4 `/llm/similar_errors` 复用：

```python
def score_query(entries: list, text: str, level: str = '') -> list:
    """token 重叠打分，返回按分数降序的 (score, entry) 列表。
    已在 /query 路由中内联实现，此处提取为函数供 llm_bp 调用。"""
    ...
```

### 2.8 前端架构

- **位置**：Inline JS，写在模板末尾的 `{% if llm_enabled %}<script>...</script>{% endif %}` 块内，与现有 `deleteKbEntry()` / `runQuery()` 风格一致
- **XSS 防护**：复用现有 `esc()` 函数对所有 LLM 返回内容转义后再插入 DOM
- **Loading 状态**：按钮 disabled + 文字变"处理中…"，finally 块中恢复
- **模态框（P3/P5）**：预渲染 HTML，CSS class 控制显隐（`.ai-modal.visible`）
- **P7 进度**：轮询 `/llm/kb_review_status` 或 SSE（与现有 `/progress/<job_id>` 风格一致）

---

## 三、LLM 嵌入点设计

按用户价值排序，优先级从高到低。

### P0 — 架构基础设施（必须最先实现）

**功能**：LLM 客户端和配置管理
**位置**：新增 `core/llm_client.py`
**影响**：所有后续功能的基础，未配置时零开销

---

### P1 — 未匹配错误自动分析（最高价值）

**痛点**：每条未匹配需手工填写 5 个字段，耗时 2~5 分钟
**位置**：`templates/result.html` → `match-box.unmatched` 区块

#### 交互设计

```
未匹配错误卡片
├── [❌ 未匹配]
├── [🤖 AI 分析] ← {% if llm_enabled %}
│   └── 点击后：
│       → 按钮变"分析中…"，disabled
│       → POST /llm/analyze_error
│       → 成功：预填5个字段，表单上方显示「AI 建议（请审核后再写入）」浅黄提示条
│         （空字段不预填，保持原始空白）
│       → 失败：按钮恢复，表单保持空白（静默降级）
└── [手动填写回写表单...]
```

#### 路由

```
POST /llm/analyze_error
请求：{ file_name, error_idx, level, error_id, location, description }
响应：
  成功：{ ok:true, keywords, reason, category, solution, module }
  失败：{ ok:false, reason:"..." }
```

> `file_name` + `error_idx` 用于结合 `state._get_results()` 做可选上下文扩充（当前错误的上下文行），与现有 `/writeback` 参数设计对齐。

#### Prompt

```
System: 你是一名经验丰富的芯片验证工程师，专注于 UVM 仿真日志分析。
        请严格以 JSON 格式回答，不包含任何其他文字。
        信息不足的字段返回空字符串 ""，不要编造内容。

User:   分析以下 UVM 仿真错误，返回 JSON：
        {
          "keywords": "3~5个关键词，英文逗号分隔，最能定位此错误的技术词汇",
          "reason":   "根本原因，信息不足返回 """,
          "category": "DUT Bug | TB Bug | 用例问题 | 工具问题 | 其他问题",
          "solution": "建议解决方案，信息不足返回 """,
          "module":   "从 location 推断的出错模块名，无法判断返回 """
        }

        错误级别：{level}
        错误 ID：{error_id}
        位置：{location}
        描述：{description}

temperature=0.2, max_tokens=400
```

#### 解析容错

```python
m = re.search(r'\{.*\}', content, re.DOTALL)
data = json.loads(m.group()) if m else {}
# category 模糊匹配 _VALID_CATEGORIES 枚举，无匹配默认「其他问题」
# （复用 blueprints/writeback.py 中已有的枚举归一化逻辑）
```

---

### P2 — 多条匹配智能推荐

**痛点**：命中多条 KB 条目时，按"录入日期降序"可能不是最相关的
**位置**：`templates/result.html` → 已匹配错误卡片（`entries.length > 1` 时显示）

#### 交互设计

```
已匹配错误卡片（entries.length > 1 时）：
├── [共 N 条]  [🤖 智能推荐] ← {% if llm_enabled and entries|length > 1 %}
│
│   默认：按录入日期降序（服务端现有行为，不变）
│   点击"智能推荐"后：
│       → POST /llm/rank_entries
│       → 成功：
│           ① 条目按 LLM 评分重排，每条显示推荐理由标签（.ai-reason-tag）
│           ② 匹配框顶部新增「🎯 重点关注用例」面板（蓝色边框徽章列表）
│              - focus_cases 非空：显示用例名徽章列表
│              - focus_cases 为空：显示「暂无关联用例」灰色提示
│       → 按钮变「✅ 已推荐」+ [恢复默认] 按钮
│       → 点击"恢复默认"：DOM 恢复原顺序，隐藏用例面板
│       → 失败：静默保持原顺序
```

#### 路由

```
POST /llm/rank_entries
请求：{ entries:[{_row_idx, 错误ID, 报错原因, 解决方案, 关键描述关键词, 关联用例, ...}],
        current_error:{level, error_id, location, description} }
响应：
  成功：{ ok:true, ranked:[0,2,1,...], reasons:["理由",...],
          focus_cases:["tc_xxx",...] }   ← 去重，≤5 条，无则 []
  失败：{ ok:false, reason:"..." }
```

#### Prompt

```
System: 你是一名经验丰富的芯片验证工程师。

User:   根据以下当前错误，对候选知识库条目按相关性从高到低排序，说明每条推荐理由。

        当前错误：
        级别：{level} | 错误ID：{error_id}
        位置：{location}
        描述：{description[:500]}

        候选条目（共{N}条）：
        [0] ID:{错误ID} | 模块:{所属模块} | 原因:{报错原因[:80]} | 关键词:{关键描述关键词} | 用例:{关联用例}
        [1] ...

        返回 JSON：
        {
          "ranked":      [2, 0, 1, ...],
          "reasons":     ["条目[2]推荐原因（≤30字）", ...],
          "focus_cases": ["tc_xxx", "tc_yyy"]
        }

        focus_cases：从所有候选条目的「关联用例」中挑选与当前错误最相关的
        优先回归验证用例（去重，按重要性排序，最多5条）。全为空则返回 []。

temperature=0.2, max_tokens=600
```

---

### P3 — 自定义提取功能

**痛点**：需要按自然语言从大文件提取特定内容，且要求不遗漏、不乱说
**位置**：`templates/result.html` → 顶部 `.top-actions` 按钮区
**限制**：**仅 Path 模式 + 单文件分析时显示**（Upload 模式文件已删除）

> `state._store[sid]['file_paths']` 在路径模式分析时已由 `_run_analysis` 写入（v2.1 已实现）。

#### 核心设计原则

| 约束 | 设计决策 |
|---|---|
| 128K/1M 上下文 | 2000 行片段 ≈ 75K tokens，128K 完全容纳，**无需采样** |
| 有用信息是连续块 | 找到该块边界，**整块发送**，中间不跳行 |
| 文件大小不固定 | Python 侧**预扫描**自动定位，不依赖用户指定行号 |
| 用户不知道行号 | 预扫描按错误ID/关键词锚定，滑动窗口找密度最高区 |
| 不能乱说 | System Prompt **3 条强制规则** + 每条结论**必须标注行号** |
| 不能遗漏 | 整块发送（无跳行）+ 超出上限时明确**覆盖警告** |

#### 交互设计

```
顶部栏（Path 模式 + 单文件时）：
[← 重新上传] [⬇ Excel] [⬇ HTML]  [📋 自定义提取] ← {% if llm_enabled and is_path_mode and is_single_file %}

点击后弹出模态框：
  ┌─────────────────────────────────────────────┐
  │ 📋 自定义提取  [第N轮]  [清空对话]  ✕        │
  ├─────────────────────────────────────────────┤
  │ [对话历史区] 用户消息 / AI 结果交替展示       │
  │   [覆盖警告横幅] ← 有截断时显示              │
  ├─────────────────────────────────────────────┤
  │ 行号范围：[起始]~[结束]（可选，留空自动定位）  │
  │ 查询：[_______________________] [发送]       │
  └─────────────────────────────────────────────┘

→ 后端从 state._get_results(sid)[0] 取 file_paths[0]
→ 首轮：Python 预扫描定位片段 → 整块嵌入 messages[0]（带 L行号 | 前缀）
→ 后续轮：只追加用户查询，不重传日志（大上下文无需裁剪历史）
→ LLM 返回含合法 JSON → 渲染为表格 + [导出]
  否则 → 纯文本 + [复制]
→ [清空对话]：调用 POST /llm/custom_extract 携带 clear:true
```

#### Python 预扫描：自动定位相关片段

**目标**：从任意大小的文件中，找出包含最多错误/查询关键词的连续 N 行窗口。

**流程**（单次流式扫描，内存占用固定）：

```
扫描文件，逐行读取：
  → 记录所有 UVM_FATAL/ERROR/WARNING 行号（复用 _UVM_PATTERN）
  → 记录所有 extra_patterns 匹配行号
  → 记录所有查询关键词匹配行号（case-insensitive）

构建锚定行号集合（anchor_nos）：
  → 优先使用：查询关键词匹配行号（用户明确想查什么）
  → 兜底使用：UVM 错误行号（没有关键词时，聚焦在错误集中区）

用滑动窗口找密度最高的连续 p3_max_lines 行区间：
  → 返回 (window_start, window_end)

从文件提取该区间的所有行（完整，不跳行），每行加 "L行号 | " 前缀
```

**查询关键词提取**（Python 侧，不调 LLM）：

```
从用户查询文本中提取：
  → 引号内的短语（精确匹配）
  → 形似错误ID的词（全大写+数字+下划线，如 ERR_001、TIMEOUT）
  → 剩余词语（直接用于 in 匹配）
```

**用户手动指定行号时**：跳过预扫描，直接读取 `[line_start, line_end]` 区间，超出 `p3_max_lines` 则从 `line_start` 截断并返回覆盖警告。

#### Token 预算安全检查（防止 API 上下文超限）

预扫描确定片段后、发送前执行一次字符预算检查，确保内容不超过模型上下文限制：

```
P3_OVERHEAD_TOKENS = 800   # 固定开销：System Prompt (~200) + User Message 模板 (~100)
                           # + 多轮历史消耗估算 (~500)，硬编码，无需配置

max_safe_chars = (context_window - P3_OVERHEAD_TOKENS) × p3_chars_per_token

if len(extracted_content) > max_safe_chars:
    # 计算实际可发送的行数
    safe_line_count = max_safe_chars // avg_chars_per_line   # avg 从已提取行计算
    # 以密度最高点为中心，对称收缩窗口端点
    center = (window_start + window_end) // 2
    actual_start = center - safe_line_count // 2
    actual_end   = actual_start + safe_line_count
    # 追加 coverage_warning，说明因 token 预算被进一步截断
```

**关键性质**：收缩只移动窗口端点，中间内容**不跳行**，"完整连续片段"原则不变。

**与 `p3_max_lines` 的关系**：

| 场景 | 生效限制 | 说明 |
|---|---|---|
| `p3_max_lines` 触发 | 行数上限先到 | 典型：128K 模型，`p3_max_lines=2500` 约 75K tokens，远小于 128K |
| `context_window` 触发 | token 预算先到 | 典型：8K 模型，即使 `p3_max_lines=200`，200行也可能超 8K |
| 两者都配置合理 | `p3_max_lines` 先到 | 大模型正常用法，安全检查作为兜底从不触发 |

**小上下文模型推荐配置**（`p3_chars_per_token=4`，每行均约 120 字符）：

| 模型上下文 | `context_window` | `p3_max_lines` 推荐值 | 实际可发行数（安全检查后）|
|---|---|---|---|
| 8K | 8192 | 150 | ≤ 150 行（≈18K chars ÷ 120 = 150 行，与 p3_max_lines 对齐）|
| 32K | 32768 | 600 | ≤ 600 行 |
| 128K | 128000 | 2500 | ≤ 2500 行（安全检查通常不触发）|
| 1M | 1000000 | 20000 | ≤ 20000 行（安全检查通常不触发）|

> 小模型下 `p3_max_lines` 仍有意义：它控制预扫描"找最密集的多少行"的搜索粒度。若设置过大，预扫描会找一个很大的候选窗口，然后安全检查再收缩——效果正确但多了一步收缩。建议两个字段一起配置，让 `p3_max_lines` ≈ 安全检查后实际行数，避免误导。

#### 发送给 LLM 的内容格式

每行内容带行号前缀，方便 LLM 引用，方便用户核查：

```
L45000 | UVM_ERROR /tb.sv(142) @ 1000ns: uvm_test_top.env [ERR_001] Transaction timeout
L45001 |   Expected response within 100ns window, got timeout at cycle 1234
L45002 | UVM_ERROR /tb.sv(156) @ 1001ns: uvm_test_top.env [ERR_002] Invalid addr
...
L47200 | UVM_FATAL /tb.sv(890) @ 2.5us: uvm_test_top [FATAL_MEM] Illegal memory access
```

#### Prompt 设计

**System Prompt（每次请求都发，不变）**：

```
你是一名芯片验证工程师助手，专注于 UVM 仿真日志分析。

严格规则（必须遵守）：
1. 只能基于下方「日志内容」回答，不得推断、补充或编造日志中不存在的信息
2. 如果所问信息在日志中找不到，必须明确回答「日志中未找到相关内容」，不得猜测
3. 每条结论后必须标注原始行号，格式：（L行号） 或 L行号
```

**首轮 User Message**：

```
以下是日志文件「{filename}」的部分内容，
提取范围：第 {start} 行 ～ 第 {end} 行，共 {N} 行。
{coverage_warning_if_any}

<日志内容>
L45000 | UVM_ERROR ...
...
L47200 | UVM_FATAL ...
</日志内容>

查询：{user_query}

{format_instruction}
```

**后续轮 User Message**（不重发日志，LLM 已有上下文）：

```
{user_query}

{format_instruction}
```

**`format_instruction` 自动判断**（Python 侧，基于查询文本）：

| 查询意图关键词 | format_instruction |
|---|---|
| 列出 / 汇总 / 统计 / 所有 / 枚举 | 请以 JSON 数组返回，每条包含必要字段和 `line` 行号字段。只包含日志中明确出现的值，缺失字段填 null。 |
| 分析 / 描述 / 发生 / 为什么 / 解释 | 请用中文描述，每个结论后用（L行号）标注日志原文出处。 |
| 其他/不确定 | 请根据查询意图选择合适格式（JSON 或文字），所有结论必须标注行号来源。 |

#### 多轮对话设计（大上下文优化）

```
第1轮：
  Python 预扫描 → 提取片段 → 嵌入 User Message → 发送给 LLM
  p3_history = [
    {role:"user", content:"<日志内容>...\n查询：xxx"},
    {role:"assistant", content:"...（含行号引用）"}
  ]

第2轮（追问同一片段）：
  直接追加新问题，无需重新扫描
  发送：[首轮消息, 首轮回复, {role:"user", content:"追问"}]

触发重新提取的条件：
  - 用户点击 [清空对话]
  - 用户查询包含明显不同的错误ID/关键词（Python 判断当前片段中找不到）
  - 用户在查询中指定了新的行号范围

历史裁剪：
  - 128K + 2500行：基本不超限，无需裁剪
  - 1M + 20000行：几乎不超限
  - 极端情况（>10轮且回复很长）：丢弃最早的非首轮对话，保留首轮 log
```

#### 路由

```
POST /llm/custom_extract
请求：{
  query:        str,
  line_start:   int | null,    // 用户手动指定时覆盖自动定位
  line_end:     int | null,
  level_filter: str | null,
  clear:        bool            // true 时重置 p3_history
}
  file_path 不传，后端从 state._get_results(sid) 取 file_paths[0]

响应：
  成功：{ ok:true, format:"json"|"text", data:...,
          extracted_start:int, extracted_end:int,
          total_lines_sent:int, turns:int,
          coverage_warning:str|null }
  失败：{ ok:false, reason:"..." }
```

**覆盖警告示例**（相关内容跨越超过 `p3_max_lines` 时）：

```json
{
  "coverage_warning": "相关内容跨越 8000 行（第 43000~51000 行），已截取密度最高的 2500 行（第 45200~47700 行）。如需查看其他部分，请在查询中指定行号范围：「行号 45000-47000」。",
  "extracted_start": 45200,
  "extracted_end": 47700
}
```

---

### P4 — 相似错误推荐（写回辅助）

**痛点**：规则匹配为空，但 KB 中可能存在同根因但 ID/关键词不同的条目
**位置**：`templates/result.html` → 未匹配回写表单内，折叠面板

#### 交互设计

```
回写表单内：
└── [🔍 查找相似已知错误 ▸] ← {% if llm_enabled %}
    展开时自动触发 POST /llm/similar_errors
    显示最多 5 条相似条目卡片：错误ID + 报错原因 + 解决方案 + 相似原因
    [参考此条目] → 一键复制 报错原因/解决方案/关键描述关键词 到回写表单
```

#### 路由

```
POST /llm/similar_errors
请求：{ db_path, level, error_id, description, top_k:5 }
响应：
  成功：{ ok:true, similar:[{_row_idx, 错误ID, 报错原因, 解决方案, 关键描述关键词, similarity_reason}] }
  失败：{ ok:false, similar:[] }
```

#### 实现要点

- 先用 `matcher.score_query(entries, description)` 筛选 top-50 候选（token 重叠预筛，不是随机取50行）
- LLM 返回的 `idx` 是批次内索引，映射回候选列表的 `_row_idx`
- `db_path` 通过 `state._validate_db_path()` 验证（与现有 KB 路由保持一致）

#### Prompt

```
System: 你是一名经验丰富的芯片验证工程师。

User:   以下是一个尚未匹配的 UVM 错误：
        级别：{level} | 错误ID：{error_id}
        描述：{description[:500]}

        以下是知识库候选条目（共{N}条，已按关键词相关度预筛选）：
        [0] ID:{错误ID} | 原因:{报错原因[:80]} | 方案:{解决方案[:60]} | 关键词:{关键描述关键词}
        [1] ...

        请找出与当前错误根因相同或高度相似的条目（最多5条），无则返回空列表。
        返回 JSON：{"similar": [{"idx": 0, "reason": "≤30字相似原因"}]}

temperature=0.2, max_tokens=400
```

---

### P5 — 批量错误模式分析

**痛点**：回归测试数十个 log 失败时，需人工归纳主要失败模式
**位置**：`templates/result.html` → 顶部 `.top-actions` 按钮区
**限制**：**仅多文件分析时显示**

#### 交互设计

```
顶部栏（多文件时）：
[← 重新上传] [⬇ Excel] [⬇ HTML]  [🤖 AI 模式分析] ← {% if llm_enabled and is_multi_file %}

点击后弹出模态框，显示 spinner
→ POST /llm/batch_patterns
→ 成功：展示 3~7 个模式卡片（.ai-pattern-card）：
         标题 + 涉及错误ID列表 + 影响文件数 + 模式特征 + 建议操作
→ 失败：模态框内显示错误信息
```

#### 路由

```
POST /llm/batch_patterns
请求：{}  （后端从 session 读取 state._get_results(sid)）
响应：
  成功：{ ok:true, total_files:N,
          patterns:[{title, error_ids:[], file_count:N, description, suggested_action}] }
  失败：{ ok:false, reason:"..." }
```

#### 数据聚合逻辑（后端，无需 LLM）

```python
# 遍历所有文件 top_errors，按 (level, error_id) 去重统计
seen = {}   # key: (level, error_id) → {'file_count': int, 'description': str}
for file_result in results:
    for err in file_result.get('top_errors', []):
        key = (err['level'], err['error_id'])
        if key not in seen:
            seen[key] = {'file_count': 0, 'description': err['description']}
        seen[key]['file_count'] += 1
# 按 file_count 降序取前 20 条送 LLM
top20 = sorted(seen.items(), key=lambda x: -x[1]['file_count'])[:20]
```

#### Prompt

```
System: 你是一名日志分析专家，擅长归纳批量测试失败的根本原因模式。
        请严格以 JSON 格式回答。

User:   以下是一批回归测试的失败错误统计（已去重，按出现文件数降序）：

        [1] 级别:{level} | ID:{error_id} | 出现{file_count}个文件 | 描述:{description[:150]}
        [2] ...

        请归纳 3~7 个主要失败模式，返回 JSON：
        {
          "patterns": [
            {
              "title":            "一句话模式标题",
              "error_ids":        ["ID1", "ID2"],
              "file_count":       15,
              "description":      "模式特征说明",
              "suggested_action": "建议排查方向"
            }
          ]
        }

temperature=0.3, max_tokens=800
```

---

### P6 — 语义知识库查询增强

**痛点**：现有 token 重叠算法无法识别同义词和中文近义表达
**位置**：`templates/index.html` → 查询 Tab 按钮区

#### 交互设计

```
查询 Tab：[查询]  [🔍 语义搜索] ← {% if llm_enabled %}

一键操作（用户只需点一次）：
1. 前端自动先调 /query 获取规则匹配候选（最多 30 条）
2. 候选 ≤1 条：toast 提示「规则查询无结果，请调整搜索条件」，不调 LLM
3. 候选 >1 条：将候选 + 查询文本发给 /llm/semantic_query 重排序
4. 前端按返回顺序重排 .qr-card 卡片，每张卡片下方显示相关性说明
```

#### 路由

```
POST /llm/semantic_query
请求：{ db_path, level, text,
        candidates:[{_row_idx, 错误ID, 错误类型, 报错原因, 关键描述关键词}] }
响应：
  成功：{ ok:true, ranked:[int,...], reasons:["相关性说明",...] }
  失败：{ ok:false }
```

#### Prompt

```
System: 你是一名知识库搜索专家。请根据用户查询文本对候选结果按语义相关性重排。
        仅返回 JSON，不包含其他文字。

User:   用户查询：{text}

        候选结果（共{N}条，已按关键词初步筛选）：
        [0] ID:{错误ID} | 级别:{错误类型} | 原因:{报错原因[:80]} | 关键词:{关键描述关键词}
        [1] ...

        按语义相关性从高到低排序，说明每条相关性原因（≤20字）：
        {"ranked":[2,0,1,...], "reasons":["相关性原因",...]}

temperature=0.1, max_tokens=300
```

---

### P7 — 知识库语义去重质量检查

**痛点**：规则去重基于精确字符串，无法识别措辞不同的同义条目
**位置**：`templates/index.html` → 「添加条目」Tab 底部

> 操作按钮调用现有 `/kb/delete` 端点（v2.2 已有撤销删除保护，误删可恢复）。

#### 交互设计

```
「添加条目」Tab 底部：
└── [🔍 知识库质量检查] ← {% if llm_enabled %}

点击后弹出模式选择框：
  ┌──────────────────────────────────────────┐
  │ 选择检查模式                               │
  │ ● 快速模式（~5分钟）                        │
  │   滑动窗口=20，步长=10，按错误类型分组        │
  │ ○ 深度模式（~15分钟）                       │
  │   按类型全量，自动分批50条                   │
  │                  [开始检查]  [取消]         │
  └──────────────────────────────────────────┘

检查中（进度轮询 /llm/kb_review_status）：
  正在检查 UVM_ERROR（第 3/8 组，已完成 60 对，预计剩余 8 分钟）[停止]

检查完成：
  发现 12 对疑似重复条目
  [查看结果列表] [导出 Excel]

结果列表（表格形式）：
  | 条目A（错误ID+原因摘要）| 条目B（错误ID+原因摘要）| 相似原因 | [保留两者][删除A][删除B] |
  → 删除操作调用现有 /kb/delete（v2.2 有 Toast 撤销，误删可恢复）
```

#### 路由

```
POST /llm/kb_review
请求：{ db_path, mode:"fast"|"deep", max_check:200 }
响应：{ ok:true, job_id:str }   ← 立即返回，后台执行

GET /llm/kb_review_status?job_id=xxx
响应：
  进行中：{ status:"running", group:"UVM_ERROR", done:6, total:20, eta_min:8 }
  完成：  { status:"done", suspect_pairs:[{row_a, row_b, similarity_reason}] }
          row_a/row_b 字段：{_row_idx, 错误类型, 错误ID, 报错原因, 解决方案}
  失败：  { status:"error", reason:"..." }

GET /llm/kb_review_export?job_id=xxx
响应：Excel 文件（3个 Sheet，见下）
```

#### Excel 导出（3 个 Sheet）

- **Sheet1**：疑似重复对列表
  列：序号 | 行号A | 错误类型A | 错误ID-A | 报错原因摘要A | 行号B | 错误类型B | 错误ID-B | 报错原因摘要B | 相似原因
- **Sheet2**：条目A完整数据（全部10列 KB 字段）
- **Sheet3**：条目B完整数据（全部10列 KB 字段）

Sheet2/Sheet3 行序与 Sheet1 一一对应，方便 vlookup 联查。

#### Prompt

```
System: 你是一名知识库维护专家。请找出以下条目中描述同一根因的重复对。
        仅返回 JSON，无重复时返回 {"pairs": []}。

User:   错误类型「{level}」的知识库条目（共{N}条）：

        [0] row:{_row_idx} | ID:{错误ID} | 原因:{报错原因[:80]} | 方案:{解决方案[:60]}
        [1] ...

        返回：{"pairs": [{"a":0, "b":3, "reason":"≤30字相似原因"}]}
        （a/b 为列表索引，不是 row 号）

temperature=0.1, max_tokens=500
```

#### 双模式实现

```
快速模式（fast）：
  按 错误类型 分组（UVM_ERROR/UVM_FATAL/... + extra_patterns 各自一组）
  每组用滑动窗口（window_size=kb_review_window_size，step=kb_review_step_size）
  跨窗口相同 pair 用 frozenset({row_a, row_b}) 去重

深度模式（deep）：
  按 错误类型 分组
  每组全量，按 kb_review_batch_size（默认50）分批
  不同类型组可并行处理（threading.Thread）
  LLM 返回的 a/b 映射回批次内实际 _row_idx
```

**分组依据**：`state.EXTRA_PATTERNS` 动态列出当前所有合法 level，与 `state._valid_levels()` 保持一致。

#### P7 降级策略

| 场景 | 行为 |
|---|---|
| 检查中用户停止 | 已完成批次的结果仍可导出 |
| 单批 LLM 失败 | 跳过该批，继续下一批，响应记录 `skipped_batches` |
| 深度模式 Token 超限 | 自动减小 `batch_size` 至 30，再失败则跳过该批 |
| KB 条目数 ≤ 1（同类型）| 直接跳过该类型组 |

---

## 四、工程增强功能（`core/llm_client.py` 内置）

### 1. LLM 响应缓存

- **存储**：模块级 `_cache dict`（纯内存，重启清空）
- **Key**：`md5(prompt.encode()).hexdigest()[:16]`
- **TTL**：`cache_ttl` 秒，`cache_ttl=0` 禁用
- **接口**：通过 `call_llm_with_cache()` 使用；P7 质量检查等幂等场景推荐使用

### 2. 超时重试

- **策略**：指数退避，延迟 = `llm_retry_delay × 2^attempt`
- **次数**：最多 `llm_max_retries` 次（默认2）
- **失败处理**：最终失败返回 `""`，不抛异常，调用方静默降级

### 3. 配置热重载

- **路由**：`POST /llm/reload_config`（在 `blueprints/llm_bp.py` 中）
  - 调用 `llm_client.reload_config()` 重读 `llm_config.json`
  - 更新 `app.jinja_env.globals['llm_enabled']`
  - 返回 `{ok:true, llm_enabled:bool, model:str}`
- **前端**：在 `templates/index.html` 「解析配置」Tab 底部新增「🔄 重载 LLM 配置」按钮（与现有备份面板同区域），点击后 toast 提示结果
- **用途**：修改 `llm_config.json` 后无需重启服务即可生效

---

## 五、文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `core/llm_client.py` | **新增** | LLM API 客户端，无 Flask 依赖（约 120 行）|
| `blueprints/llm_bp.py` | **新增** | Flask Blueprint，10 条 LLM 路由（约 380 行）|
| `core/matcher.py` | **修改** | 新增 `score_query()` 函数（从 `/query` 路由内联逻辑提取）|
| `app.py` | **修改** | 3 处最小改动：llm_client.init、Blueprint 注册、Jinja 全局注入 |
| `templates/result.html` | **修改** | P1/P2/P3/P4/P5 按钮 + JS（`{% if llm_enabled %}` 块）|
| `templates/index.html` | **修改** | P6/P7 按钮 + JS + 热重载按钮（`{% if llm_enabled %}` 块）|
| `static/style.css` | **修改** | 新增 AI 相关 CSS 类 |

> **无需** 新增 `core/session_store.py`（旧方案遗留；`state.py` 已完整承担该职责，且 `_store` 中的 `file_paths`/`p3_history`/`p3_tokens` 字段已在 v2.1 预留）。

### 新增 CSS 类

| 类名 | 用途 |
|------|------|
| `.btn-ai`, `.btn-ai-sm` | AI 操作按钮 |
| `.ai-hint` | P1 AI 建议提示条（浅黄色）|
| `.ai-rank-btn` | P2 智能推荐按钮 |
| `.ai-ranked-entry` | P2 推荐条目高亮（绿色边框）|
| `.ai-focus-cases` | P2 重点关注用例面板（蓝色边框徽章列表）|
| `.ai-reason-tag` | P2/P4/P6 相关性/推荐理由标签 |
| `.ai-extract-modal` | P3 自定义提取模态框 |
| `.ai-similar-section` | P4 相似错误折叠容器 |
| `.ai-suggest-card` | P4 相似错误卡片 |
| `.ai-pattern-modal` | P5 批量模式分析模态框 |
| `.ai-pattern-card` | P5 模式分析结果卡片 |
| `.ai-modal`, `.ai-modal-backdrop` | 通用模态框底层 CSS |

### 新增路由汇总（共 10 条）

| 路由 | 对应功能 |
|------|---------|
| `POST /llm/analyze_error` | P1 未匹配自动分析 |
| `POST /llm/rank_entries` | P2 多条匹配智能推荐 |
| `POST /llm/custom_extract` | P3 自定义提取 |
| `POST /llm/similar_errors` | P4 相似错误推荐 |
| `POST /llm/batch_patterns` | P5 批量模式分析 |
| `POST /llm/semantic_query` | P6 语义知识库查询 |
| `POST /llm/kb_review` | P7 启动知识库质量检查（后台任务） |
| `GET  /llm/kb_review_status` | P7 轮询检查进度 |
| `GET  /llm/kb_review_export` | P7 导出去重结果 Excel |
| `POST /llm/reload_config` | 配置热重载 |

---

## 六、降级与安全策略

| 场景 | 行为 |
|------|------|
| `llm_config.json` 不存在 | `is_configured()=False`，Jinja 不渲染任何 AI 按钮，功能与现在完全一致 |
| LLM 接口超时 | 路由返回 `{ok:false}`，前端按钮恢复可用，不影响手工流程 |
| LLM 返回非 JSON | 正则提取 `\{.*\}` 块，失败返回 `{ok:false}` |
| LLM 返回非法 category（P1）| 复用 `blueprints/writeback.py` 已有枚举归一化，无匹配默认「其他问题」|
| KB 为空（P4/P7）| 直接返回 `{ok:true, similar:[]}` 或 `{ok:true, suspect_pairs:[]}`|
| entries.length ≤ 1（P2）| 不显示「智能推荐」按钮 |
| focus_cases 全为空（P2）| 面板显示「暂无关联用例」灰色提示，不隐藏面板 |
| Upload 模式（P3）| 不显示「自定义提取」按钮 |
| 单文件模式（P5）| 不显示「AI 模式分析」按钮 |
| 语义查询候选 ≤1（P6）| toast 提示，不调 LLM |
| LLM 响应命中缓存 | 直接返回缓存内容 |
| P7 删除操作 | 调用现有 `/kb/delete`，v2.2 已有 Toast 撤销保护，误删 8 秒内可恢复 |
| llm_config.json 修改后 | 调用 `POST /llm/reload_config`，无需重启 |

---

## 七、实施顺序

| 步骤 | 内容 | 依赖 | 备注 |
|------|------|------|------|
| 1 | `core/matcher.py` — 提取 `score_query()` | 无 | 从 `blueprints/kb.py` `/query` 路由内联逻辑抽取 |
| 2 | `core/llm_client.py` — LLM API 客户端 | 无 | |
| 3 | `blueprints/llm_bp.py` — Blueprint + 路由骨架 | 步骤 1/2 | 先实现 P0 热重载，验证整体链路 |
| 4 | `app.py` — 3 处最小改动 | 步骤 2/3 | |
| 5 | P1: `analyze_error` + `result.html` AI 分析按钮 | 步骤 3/4 | 价值最高，优先验证 |
| 6 | P2: `rank_entries` + DOM 重排 | 步骤 3/4 | |
| 7 | P4: `similar_errors` + 折叠面板 | 步骤 1/3/4 | 依赖 score_query |
| 8 | P5: `batch_patterns` + 模态框 | 步骤 3/4 | |
| 9 | P3: `custom_extract` + Python 预扫描 + 多轮对话 | 步骤 3/4 | p3_history 字段已在 state.py 预留；预扫描为单次流式扫描，内存占用固定 |
| 10 | P6: `semantic_query` + 查询 Tab 联动 | 步骤 1/3/4 | |
| 11 | P7: `kb_review` + 进度轮询 + 导出 | 步骤 2/3/4 | 复杂度最高，放最后 |
| 12 | `static/style.css` — AI CSS 类 | 步骤 5~11 | 随各功能同步添加 |

步骤 1/2 无互相依赖，可并行实施。

---

## 八、验证要点

1. **基础版验证**：无 `llm_config.json` 时，启动工具，所有页面不显示任何 AI 字样按钮
2. **P1**：未匹配错误一键分析，5 个字段正确预填；空字段不预填；LLM 超时后表单保持空白
3. **P2**：命中多条 → 重排 + 推荐理由；有关联用例 → 用例面板展示；恢复默认 → 顺序还原 + 面板隐藏
4. **P3**：
   - Path 模式单文件显示按钮，Upload 模式不显示
   - 无关键词时聚焦 UVM 错误密度最高区，有关键词时按关键词锚定
   - 发送内容为完整连续片段（无跳行），每行带 `L行号 |` 前缀
   - 多轮追问不重传日志，历史正确追加；`coverage_warning` 超出时正确返回并展示
   - **小上下文模型**：内容超出 `context_window` token 预算时，以密度中心对称收缩窗口端点（不跳行），追加 `coverage_warning`，API 调用不报错
5. **P7 删除流程**：删除疑似重复条目后，Toast 8 秒内可撤销（v2.2 已有基础设施）
6. **降级验证**：LLM 超时/失败，不影响任何现有手工流程
7. **回归验证**：现有 `/kb/*`、`/analyze`、`/writeback` 等路由行为完全不变
