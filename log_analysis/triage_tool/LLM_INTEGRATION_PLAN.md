# triage_tool — LLM 集成方案 v4.0（已实现）

> **状态**：功能已全部实现并集成到主工具中。本文档描述实际架构与实现细节，作为维护参考。
> **版本说明**：v4.0 在 v3.2 基础上新增 P0 配置 GUI（消除手动编辑 JSON 的门槛）、增强/基础模式切换、Anthropic API 格式兼容、P6 AI 辅助合并（Merge Modal），并将 HTTP 层从 `requests` 替换为 stdlib `urllib`（适配零依赖内网部署）。
> **v4.1**：P1 推荐用例新增 Testlist 导出功能——从 `focus_cases` 一键生成回归 Testlist，支持批量/单条参数配置、浏览器内预览、复制文本、下载（Chrome/Edge 可选保存路径，Firefox 降级到默认目录）。
> **v4.2**：P2 新增「日志原文侧边栏 + 导出」——Modal 改为左右分栏，右侧常驻展示 AI 参考的 log 原文（含原始行号），支持单段导出（`log_X_Y.log`）和全轮次合并导出（`log_all_turns.log`）。
> **v4.3**：移除「未匹配错误自动分析」（原 P1，AI 推断 5 字段预填回写表单）——日志原文用户自有，AI 推断价值低且需人工核对，反而增加流程；后续 P 编号顺延（原 P2~P7 → P1~P6）。

---

## Context

triage_tool 是一个基于规则的 UVM 仿真日志分类分诊工具，在规则引擎基础上叠加了可选的 LLM 增强层：

- **基础版**（无 LLM）：与原工具完全一致，所有 AI 按钮隐藏
- **增强版**（含 LLM）：AI Tab 始终显示，配置 LLM 后解锁 P1~P6 全部 AI 功能

两种模式可在界面中一键切换，选择持久化到 `localStorage`，刷新后保留。

---

## 一、版本差异对比

| 功能模块 | 基础版（无 LLM） | 增强版（含 LLM） |
|---------|-----------------|-----------------|
| **多条匹配** | 按录入日期降序展示 | P1：LLM 按相关性重排 + 推荐理由 + 重点关注用例列表 |
| **自定义提取** | 不支持 | P2：自然语言查询 + 行号范围提取（Path 模式单文件） |
| **相似错误** | 无 | P3：语义相似 KB 条目推荐，辅助写回 |
| **批量分析** | 人工扫描统计 | P4：AI 自动归纳 3~7 个失败模式 |
| **知识库查询** | 关键词/ID 模糊匹配 | P5：语义搜索（规则预筛选 + LLM 重排，全库兜底） |
| **知识库维护** | 基于字符串规则去重 | P6：AI 语义重复检测 + AI 辅助合并（Merge Modal） |

---

## 二、核心架构

### 2.1 目录结构（已实现）

```
log_analysis/triage_tool/
├── app.py                  # Flask 入口，注册 Blueprint + 初始化 llm_client
├── state.py                # 共享状态（_store、_jobs、EXTRA_PATTERNS 等）
├── blueprints/
│   ├── analysis.py         # /analyze、/progress、/result 等
│   ├── writeback.py        # /writeback
│   ├── kb.py               # /kb/* 知识库管理
│   ├── config_bp.py        # /extra_patterns/*、/pass_patterns/*
│   ├── export.py           # /export/*
│   └── llm_bp.py           # ← 新增：14 条 LLM 路由
├── core/
│   ├── log_parser.py       # 流式解析
│   ├── matcher.py          # 两阶段匹配 + score_query()
│   ├── db_manager.py       # Excel 读写 + 锁 + 备份
│   ├── reporter.py         # 报告生成
│   └── llm_client.py       # ← 新增：LLM API 客户端（纯 stdlib）
└── templates/ static/
    ├── index.html          # 修改：AI Tab + P5/P6 + Config GUI
    └── result.html         # 修改：P1/P2/P3/P4 + 模式切换按钮
```

### 2.2 app.py 改动（3 处）

```python
# 1. 初始化 LLM 客户端（在 BASE_DIR 确定后）
from core import llm_client
llm_client.init(BASE_DIR)

# 2. 注册 LLM Blueprint
from blueprints.llm_bp import llm_bp
app.register_blueprint(llm_bp)

# 3. 注入 Jinja 全局变量（llm_enabled 初始值）
app.jinja_env.globals['llm_enabled'] = llm_client.is_configured()
# 注：配置 GUI 保存成功后在 /llm/save_config 路由内动态更新此值
```

### 2.3 `core/llm_client.py` 接口

```python
init(base_dir: Path) -> None
    # 加载 llm_config.json + 环境变量覆盖，应在 BASE_DIR 确定后调用一次

is_configured() -> bool
    # endpoint + model 均非空则 True

reload_config() -> bool
    # 热重载 llm_config.json + 环境变量，返回是否已配置

get_config() -> dict | None
    # 当前配置字典快照，未配置时返回 None

save_config(cfg: dict) -> None
    # 将 cfg 写入 llm_config.json，然后热重载

call_llm(messages, temperature=0.2, max_tokens=400) -> str
    # 调用 LLM，含指数退避重试，失败返回 ""，不抛异常
    # messages: [{"role": "user/system/assistant", "content": "..."}]

call_llm_verbose(messages, temperature=0.2, max_tokens=400) -> (str, str|None)
    # 同上，返回 (result, error_str)；成功时 error_str=None，失败时 result=""
    # 用于连接测试等需要详细错误信息的场景

call_llm_with_cache(messages, temperature=0.2, max_tokens=400) -> str
    # call_llm 的内存缓存包装；cache_ttl=0 禁用，重启清空
```

**HTTP 层**：纯 stdlib `urllib.request`，不依赖 `requests`，适配离线内网环境。
**代理绕过**：使用 `ProxyHandler({})` 阻止系统 `http_proxy` 环境变量干扰内网 LLM 服务调用。

### 2.4 Anthropic API 格式支持

`llm_client.py` 自动检测 endpoint 格式，无需用户配置：

```
检测规则：endpoint URL 中包含 "anthropic" 字符串 → Anthropic 格式

Anthropic 格式差异：
  认证头：x-api-key + anthropic-version: 2023-06-01（非 Authorization: Bearer）
  路径：自动在末尾追加 /v1/messages（用户填 base URL 即可）
  System 消息：从 messages 数组提取，移至顶层 "system" 字段（Anthropic API 要求）
  响应解析：content[0].text（非 choices[0].message.content）
```

支持的格式：
- **OpenAI 兼容**：内网 Ollama / vLLM / 各类本地部署，endpoint 以 `/v1/chat/completions` 结尾
- **Anthropic 格式**：Claude 系列（官方 API）、智谱 GLM Anthropic 兼容接口（`open.bigmodel.cn/api/anthropic`）

### 2.5 配置文件 `llm_config.json`

位置：`BASE_DIR/llm_config.json`（与 exe 同目录，不打包进 exe）。**可通过 AI 功能 Tab 中的配置 GUI 直接创建和编辑，无需手动操作文件。**

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

**环境变量覆盖（优先级高于文件）**：

| 环境变量 | 对应字段 |
|---|---|
| `LLM_ENDPOINT` | endpoint |
| `LLM_API_KEY` | api_key |
| `LLM_MODEL` | model |
| `LLM_TIMEOUT` | timeout（整数秒）|

### 2.6 增强版 / 基础版模式切换

两种模式通过 CSS class `body.llm-basic` 控制 AI 元素的显隐：

```css
body.llm-basic .llm-only { display: none !important; }
body.llm-basic .ai-modal  { display: none !important; }
/* 所有 AI 按钮添加 class="llm-only" 即可响应切换 */
```

切换逻辑：
- `localStorage.getItem('llmMode')` 读取上次选择（`'enhanced'` 或 `'basic'`）
- 页面加载时自动应用（`index.html` + `result.html` 均支持）
- LLM 调用失败（P1/P3）时，页面顶部显示黄色提示条（`.llm-fail-bar`），提示切换到基础版
- `_notifyLlmFail()` 函数连续失败 N 次后自动建议切换

---

## 三、LLM 注入点设计

### P0 — 配置 GUI 与基础设施

**已实现功能**：
- AI Tab 始终显示（不受 `llm_enabled` 控制）
- 未配置态：显示完整配置表单（endpoint / api_key / model + 高级设置折叠面板）
- 已配置态：折叠显示当前模型名 + 修改/测试/重载按钮
- 高级设置：超时秒数、上下文窗口（128K/1M/自定义预设）、AI日志问答最大行数（随窗口自动计算）
- 保存成功后动态更新 `jinja_env.globals['llm_enabled']`，无需重启

**已实现路由**：

```
GET  /llm/get_config
  响应：{ ok, configured, config: { endpoint, api_key(脱敏前4位+***), model, timeout, ... } }

POST /llm/save_config
  请求：{ endpoint, api_key, model, timeout, context_window, p3_max_lines }
  校验：endpoint 非空 + 以 http 开头；model 非空
  响应：{ ok, llm_enabled, model }

POST /llm/test_connection
  请求：{}
  响应：{ ok, elapsed_ms, model, reply }（失败：{ ok:false, reason, elapsed_ms }）

POST /llm/reload_config
  响应：{ ok, llm_enabled, model }
```

---

### P1 — 多条命中智能推荐

**位置**：`result.html` → 命中 ≥2 条的匹配块
**路由**：`POST /llm/rank_entries`
**请求**：`{ entries:[...], current_error:{level, error_id, location, description} }`
**响应**：`{ ok, ranked:[int,...], reasons:[str,...], focus_cases:[str,...] }`

条目按 LLM 评分重排，每条显示推荐理由标签，顶部展示「重点关注用例」面板。

#### P1 附加：Testlist 导出（纯前端）

`focus_cases` 已在浏览器中，无需后端。点击「📋 导出 Testlist」弹出导出面板：

**列定义（`TESTLIST_COLUMNS`，一处配置驱动所有逻辑，增删列只改此处）**：

```javascript
const TESTLIST_COLUMNS = [
  { name: 'RUN_NUM', type: 'int',  default: 1,        min: 1 },
  { name: 'WAVE',    type: 'enum', default: 'on',     options: ['off', 'on'] },
  { name: 'COV',     type: 'enum', default: 'off',    options: ['off', 'on'] },
  { name: 'SVSEED',  type: 'str',  default: 'random' },
];
```

**参数编辑规则**：
- **⚡ 批量设置行**（表头下方紫色行）：修改后立即覆盖所有用例；之后仍可对单条单独调整，单条修改优先
- int 列：失焦/change 时校验 ≥ min，非法值自动纠正为 default
- enum 列：`<select>` 限定合法值
- str 列：自由输入

**生成格式**：Tab 分隔（与 testlist demo 一致，在任何编辑器中 Tab 自动对齐）：
```
#TC_NAME	RUN_NUM	WAVE	COV	SVSEED
tc_sanity	1	on	off	random
```

**预览**：HTML `<table>` 渲染（不依赖等宽字体，浏览器自动对齐列宽）

**下载**：

| 浏览器 | 行为 |
|---|---|
| Chrome / Edge | `showSaveFilePicker` 系统对话框，可选路径 + 文件名 |
| Firefox / 其他 | 直接下载到浏览器默认目录，文件名用输入框值 |

**文件名控制**（Modal footer 两个输入框）：
- 主名输入框：默认 `regression_testlist`
- 后缀输入框：默认**空**（无后缀，适配 Linux 内网习惯），可填 `.txt`、`.list` 等
- 浏览器类型自动检测，旁边显示小字提示（`可选保存路径` 或 `保存至浏览器下载目录`）

**操作**：📋 复制文本 / ⬇ 下载 / 关闭

**文件位置**：仅修改 `templates/result.html`（纯前端，零后端改动）

---

### P2 — AI 日志问答

**位置**：`result.html` 顶栏（仅 Path 模式 + 单文件时显示）
**路由**：`POST /llm/custom_extract`
**请求**：`{ query, line_start|null, line_end|null, clear:bool }`
**响应**：`{ ok, format, data, extracted_start, extracted_end, total_lines_sent, turns, coverage_warning, raw_lines }`

**预扫描关键点**：
- `_UVM_REAL_PAT = re.compile(r'\bUVM_(?:ERROR|WARNING|FATAL)\b.*@')` — 要求 `@` 时间戳，排除文件末尾的统计汇总行
- `_query_prefers_start()` — 检测 `前\d|第[一1]|first|top\d|earliest` 等关键词，命中时窗口从第一个锚点往前 50 行开始（不用密度最高区）
- `_extract_query_keywords()` — 按 `[\s\d\u4e00-\u9fff]+` 分割提取英文/ID 关键词（如 `uvm_error`、`ERR_001`）

**Token 预算安全检查**：`P3_OVERHEAD_TOKENS=800`，超出时以密度中心对称收缩窗口（不跳行）。

#### P2 附加：日志原文侧边栏 + 导出（v4.2）

Modal 改为左右分栏：
- **左侧**：聊天区 + 输入框（不变）
- **右侧**（400px 固定宽）：`#p3Sidebar` 常驻展示 AI 参考的 log 原文

`raw_lines` 字段由后端 `_read_lines_range()` 的返回值（`[(lineno, content)]`）转换为 `[{lineno, content}]` 对象数组，随 AI 回答一起返回给前端。

**侧边栏行为**：
- 每次 AI 回答后自动刷新，滚动到顶部，标题更新为「📄 日志原文 第X~Y行」
- 行号用 6 位右对齐，灰色 `user-select:none`，不干扰内容复制

**导出**：

| 按钮 | 位置 | 文件名 | 内容 |
|---|---|---|---|
| ⬇ 导出此段 | 侧边栏头部（AI 首次回答后显示）| `log_X_Y.log` | 当前轮 log 原文，首行注释标明行范围 |
| ⬇ 导出全部日志 | Modal 顶栏（AI 首次回答后显示）| `log_all_turns.log` | 所有轮次 log 段合并，段间以 `# 第N轮 第X~Y行` 注释分隔 |

前端维护 `_p3LogHistory[]` 累积所有轮次的 `{turn, start, end, lines}`，清空对话时同步重置。

---

### P3 — 相似错误推荐

**位置**：`result.html` → 未匹配错误回写表单下方
**路由**：`POST /llm/similar_errors`
**请求**：`{ db_path, level, error_id, description, top_k:5 }`
**响应**：`{ ok, similar:[{_row_idx, 错误ID, 报错原因, 解决方案, 关键描述关键词, similarity_reason}] }`

先用 `matcher.score_query()` 筛选 top-50 候选，再发 LLM 语义筛选最多5条。

---

### P4 — 批量错误模式分析

**位置**：`result.html` 顶栏（仅多文件时显示）
**路由**：`POST /llm/batch_patterns`
**请求**：`{}` (后端从 session 读取)
**响应**：`{ ok, total_files, patterns:[{title, error_ids, file_count, description, suggested_action}] }`

后端按 `(level, error_id)` 去重统计，按出现文件数降序取 top-20 送 LLM。

---

### P5 — 语义知识库搜索

**位置**：`index.html` → AI 功能 Tab → 语义知识库查询区
**路由**：`POST /llm/semantic_query`
**请求**：`{ db_path, level, text, candidates:[...] }`
**响应**：`{ ok, ranked:[int,...], reasons:[str,...] }`

**两阶段检索**（已实现）：
1. 前端先调 `/query` 关键词预筛选（最多30条）
2. 候选为空时 → 直接读全库作为候选（兜底，确保语义搜索不因关键词缺失而失效）
3. 候选非空 → 发 LLM 语义重排

**LLM Prompt 要点**：明确要求"完全不相关的条目不放入结果，全不相关返回 `[]`"，避免 LLM 对所有候选排序。

**结果展示**：展示全部10个字段（含录入人、录入日期），前5条默认展开，有展开/折叠全部按钮。

---

### P6 — 知识库语义去重 + AI 辅助合并

**位置**：`index.html` → AI 功能 Tab → 知识库质量检查区
**路由**：
```
POST /llm/kb_review         # 启动后台任务，返回 job_id
GET  /llm/kb_review_status  # 轮询进度
GET  /llm/kb_review_export  # 导出 Excel（3个 Sheet）
POST /llm/merge_suggest     # AI 建议合并两条重复条目
```

**`/llm/merge_suggest` 设计**：
```
请求：{ row_a: {完整条目+_row_idx}, row_b: {完整条目+_row_idx} }
响应：{ ok, merged: {9个字段的合并建议} }

合并规则（Prompt 约束）：
  关键描述关键词 / 关联用例：两者合并去重，逗号分隔
  报错原因 / 解决方案：选更详细或融合补充
  其余字段：非空优先，A 优先于 B
AI 失败降级：以"A字段非空优先"作为兜底，用户仍可在表格中编辑后提交
```

**Merge Modal 交互**：

```
P6 结果卡片（每对）：
  └── [🔀 AI辅助合并] 按钮（id="p7pair-N" 用于合并后隐藏）
        ↓ 点击
      Merge Modal 弹出
        ├── 调 POST /llm/merge_suggest → AI 生成建议（约2s，弹出即请求）
        ├── 三列对比表格：条目A | 合并结果（可编辑input） | 条目B
        ├── [✅ 确认合并] → POST /kb/update(row_a, force:true) + POST /kb/delete(row_b)
        └── 成功后：Modal关闭，卡片隐藏（display:none）
```

`force: true` 跳过重复检查（防止 A 更新后与自身冲突）。
`update_entry` 不改行号，`delete_entry(row_b)` 时 row_b 行号无需调整。

**结果展示**：每对重复条目展示全部10个字段（`dict(entry, _row_idx=row_idx)` 完整存储）。

---

## 四、工程基础设施（`core/llm_client.py` 内置）

### 响应缓存

- **存储**：模块级 `_cache dict`（内存，重启清空）
- **Key**：`md5(json.dumps(messages))[:16]`（含所有字段，键排序）
- **TTL**：`cache_ttl` 秒，`0` 禁用
- **使用**：`call_llm_with_cache()`；P6 等幂等场景推荐

### 超时重试

- **策略**：指数退避，延迟 = `llm_retry_delay × 2^attempt`
- **次数**：最多 `llm_max_retries` 次（默认2）
- **失败**：返回 `""`，不抛异常，调用方静默降级

### 配置热重载

`POST /llm/reload_config` — 调用 `llm_client.reload_config()` 重读文件，更新 `jinja_env.globals['llm_enabled']`，无需重启。

---

## 五、文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `core/llm_client.py` | **新增** | LLM API 客户端，纯 stdlib urllib，约 300 行 |
| `blueprints/llm_bp.py` | **新增** | Flask Blueprint，14 条 LLM 路由，约 950 行 |
| `core/matcher.py` | **修改** | 新增 `score_query()` 函数 |
| `app.py` | **修改** | 3 处：llm_client.init、Blueprint 注册、Jinja 全局注入 |
| `templates/result.html` | **修改** | P1/P2/P3/P4 按钮 + JS + 模式切换按钮 + 失败提示条 |
| `templates/index.html` | **修改** | AI Tab（常显）+ Config GUI + P5/P6 + Merge Modal + JS |
| `static/style.css` | **修改** | 新增 AI CSS 类（见下） |

### 已实现路由汇总（共 14 条）

| 路由 | 对应功能 |
|------|---------|
| `GET  /llm/get_config` | P0 获取当前配置（api_key 脱敏）|
| `POST /llm/save_config` | P0 保存配置到 llm_config.json |
| `POST /llm/reload_config` | P0 热重载配置 |
| `POST /llm/test_connection` | P0 连接测试 |
| `POST /llm/rank_entries` | P1 多条匹配智能推荐 |
| `POST /llm/custom_extract` | P2 AI 日志问答 |
| `POST /llm/similar_errors` | P3 相似错误推荐 |
| `POST /llm/batch_patterns` | P4 批量错误模式分析 |
| `POST /llm/semantic_query` | P5 语义知识库查询重排 |
| `POST /llm/kb_review` | P6 启动知识库质量检查（后台任务）|
| `GET  /llm/kb_review_status` | P6 查询检查进度 |
| `GET  /llm/kb_review_export` | P6 导出检查结果 Excel |
| `POST /llm/merge_suggest` | P6 AI 建议合并两条重复条目 |

### 主要 CSS 类

| 类名 | 用途 |
|------|------|
| `.btn-ai`, `.btn-ai-sm` | AI 操作按钮（紫色系）|
| `.ai-hint` | AI 提示条（浅黄色，用于 LLM 配置状态、失败兜底等通用场景）|
| `.ai-reason-tag` | P1/P3/P5 推荐理由标签 |
| `.ai-modal`, `.ai-modal-backdrop`, `.ai-modal-box` | 通用模态框 |
| `.ai-modal-hdr`, `.ai-modal-footer` | 模态框头部/底部 |
| `.ai-suggest-card`, `.asc-title`, `.asc-body` | P3/P6 条目卡片 |
| `.ai-pattern-card` | P4 模式分析结果卡片 |
| `.llm-mode-btn.enhanced`, `.llm-mode-btn.basic` | 增强/基础模式切换按钮 |
| `.llm-fail-bar` | LLM 失败黄色提示条 |
| `body.llm-basic .llm-only` | 基础模式下隐藏所有 AI 元素 |

---

## 六、降级与安全策略

| 场景 | 行为 |
|------|------|
| `llm_config.json` 不存在 | `is_configured()=False`；AI Tab 显示配置表单；P5/P6 显示"请先配置"灰色提示 |
| LLM 接口超时/失败 | 路由返回 `{ok:false}`，前端按钮恢复，不影响手工流程；连续失败时提示切换基础版 |
| LLM 返回非 JSON | `_parse_json_safe()` 正则提取 `\{.*\}`，失败返回 `None`，调用方使用降级值 |
| P5 关键词过滤返回 0 条 | 直接读全库作为候选发 LLM（兜底，不显示"无结果"）|
| P5 LLM 返回空数组 | 前端显示「语义搜索未找到相关条目」（不展示无关结果）|
| Upload 模式（P2）| 不显示「AI日志问答」按钮 |
| 单文件模式（P4）| 不显示「AI 模式分析」按钮 |
| P6 合并 AI 失败 | 降级为「A字段非空优先」填入表格，用户可继续编辑后确认 |
| P6 删除后悔 | `/kb/delete` 有 Toast 8 秒撤销（`/kb/undo_delete`），误删可恢复 |
| Anthropic 404 | `_normalize_endpoint()` 自动追加 `/v1/messages`，对用户透明 |
| 系统代理干扰 | `ProxyHandler({})` 绕过 `http_proxy`，内网 LLM 直连不受代理影响 |
