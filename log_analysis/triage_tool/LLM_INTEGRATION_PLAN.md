# triage_tool — LLM 集成方案 v2.0

> **声明**：本方案为功能分析和架构设计，不含代码实现。
> **版本说明**：v2.0 在 v1.0 基础上新增 P2.5（多条匹配智能推荐）和 P2.6（自定义提取），并细化了所有场景的 Prompt 设计。

---

## Context

triage_tool 当前是一个基于规则的 UVM 仿真日志分类分诊工具。工程师在使用中面临以下痛点：

1. **规则匹配盲区**：仅支持 error_id 精确匹配 + 关键词 AND 匹配，无法识别语义相似但表述不同的错误
2. **手工填写耗时**：每条未匹配错误需手动填写 5 个字段（根因分类、解决方案等），平均 2~5 分钟/条，且依赖个人经验积累
3. **批量分析困难**：回归测试数十个 log 失败时，需人工归纳主要失败模式
4. **知识库维护成本高**：语义相似的重复条目难以及时发现和清理

**目标**：设计一套可选的 LLM 增强层，满足：
- 未配置 LLM 时，工具行为与现在 100% 一致（基础版）
- 配置 LLM 后，启用 AI 辅助功能（高配版）
- 不引入新的第三方依赖包（复用 requests 或降级到标准库 urllib）
- 支持任意 OpenAI-compatible 接口

---

## 一、版本差异对比

| 功能模块 | 基础版（无 LLM） | 高配版（含 LLM） |
|---------|-------------------|-------------------|
| **未匹配错误** | 手动填写回写表单 | P1：一键 AI 分析 + 自动预填 |
| **多条匹配** | 按录入日期降序展示 | P2.5：LLM 按相关性重排 + 推荐理由 |
| **自定义提取** | 不支持 | P2.6：自然语言查询 + 行号范围提取（Path 模式） |
| **相似错误** | 无 | P3：语义相似 KB 条目推荐，辅助写回 |
| **错误描述** | 显示原始多行文本 | P_摘要：AI 20 字摘要（折叠/展开） |
| **批量分析** | 人工扫描统计 | P4：AI 自动归纳 3~7 个失败模式 |
| **知识库查询** | 关键词/ID 模糊匹配 | P5：语义搜索（自动规则+LLM 两步合并一步） |
| **知识库维护** | 基于字符串规则去重 | P6：AI 语义重复检测（滑动窗口分批） |

---

## 二、核心架构设计

### 2.1 新增模块：`core/llm_client.py`

所有 LLM 能力的唯一入口，确保职责清晰。

```
核心接口：
├── init(base_dir: Path) → None        # 加载 llm_config.json
├── is_configured() → bool            # 检查 LLM 是否可用
├── call_llm(prompt, ...) → str       # 调用 LLM API
└── get_config() → dict | None        # 获取当前配置
```

**配置文件**（`BASE_DIR/llm_config.json`，不打包进 exe）：
```json
{
  "endpoint": "http://your-llm-server/v1/chat/completions",
  "api_key": "sk-xxx",
  "model": "qwen2.5-7b",
  "timeout": 30
}
```

**配置优先级**：**环境变量 > 文件**

| 环境变量 | 对应字段 |
|---|---|
| `LLM_ENDPOINT` | endpoint |
| `LLM_API_KEY` | api_key |
| `LLM_MODEL` | model |
| `LLM_TIMEOUT` | timeout（整数秒） |

**降级策略**：
- 优先使用 `requests`（Flask 已依赖）
- 降级使用 `urllib`（标准库兜底）
- 超时/失败时返回空字符串，静默降级

### 2.2 app.py 改动点（5~6 处最小改动）

```python
# 1. 替换 _store 相关定义 → 改为从 session_store 导入
from core.session_store import get_results, set_results   # 替换原内联定义

# 2. LLM 基础设施
from core import llm_client
from core.llm_routes import llm_bp

# 3. 初始化（在 BASE_DIR 确定后，UPLOAD_DIR.mkdir 之后）
llm_client.init(BASE_DIR)
app.jinja_env.globals['llm_enabled'] = llm_client.is_configured()
app.register_blueprint(llm_bp)

# 4. /query 路由：改用 matcher.score_query()（内联打分逻辑提取到 matcher.py）

# 5. /analyze 路由（Path 模式时）：将原始文件路径存入 session（P2.6 需要）
# set_results(sid, results, db_path, file_paths=[str(p) for p in matched_paths])
```

**关键原则**：`llm_routes.py` 不 import `app.py`（无循环依赖）；新增/修改 LLM 功能只改 `llm_routes.py` 和模板，不动基础路由。

---

## 二-B、技术架构：解耦设计

### B.1 文件结构

```
core/
  session_store.py  ← 【新增】_store + get/set_results（从 app.py 迁移）
  llm_client.py     ← 【新增】LLM API 客户端（无 Flask 依赖）
  llm_routes.py     ← 【新增】Flask Blueprint，9 条 LLM 路由
  matcher.py        ← 【修改】新增 score_query() 函数
  log_parser.py     ← 不变
  db_manager.py     ← 不变
  reporter.py       ← 不变
app.py              ← 【修改】5~6 处最小改动（见 2.2）
templates/result.html ← 【修改】{% if llm_enabled %} 包裹 AI 按钮
templates/index.html  ← 【修改】{% if llm_enabled %} 包裹 AI 按钮
static/style.css      ← 【修改】新增 AI CSS 类
```

### B.2 各新增文件说明

**`core/session_store.py`（从 app.py 迁移）**

解决循环依赖：`llm_routes.py` 需要 `_store` 数据，但 Blueprint 不能反向 import `app.py`。迁移后两者均 import `session_store`。

```python
_store: dict = {}
_STORE_TTL = 7200

def set_results(sid, results, db_path, file_paths=None):
    _store[sid] = {'results': results, 'db_path': db_path, 'ts': time.time()}
    if file_paths:
        _store[sid]['file_paths'] = file_paths  # P2.6 Path 模式需要

def get_results(sid):
    # 清理过期条目，返回当前 sid 的数据（逻辑不变）
    ...
```

**`core/matcher.py`（新增 score_query）**

将现有 `/query` 路由内联的 token 重叠打分逻辑提取为独立函数，供 `/query` 路由和 P3 `/llm/similar_errors` 共同复用。

```python
def score_query(entries, text, level=None) -> list:
    """token 重叠打分，返回按分数降序排列的 (score, entry) 列表。"""
    ...
```

**`core/llm_client.py`（纯逻辑，无 Flask 依赖）**

```
init(base_dir)          → 加载 llm_config.json，缓存配置
is_configured() → bool  → 检查 endpoint + model 是否均已设置
call_llm(prompt, ...)   → 调用 /chat/completions，失败返回 ""
```

HTTP 层：优先 `requests`（Flask 已依赖），降级 `urllib`（标准库兜底）。

**`core/llm_routes.py`（Flask Blueprint）**

```python
from flask import Blueprint, request, jsonify, session
from core import llm_client
from core.session_store import get_results
from core.matcher import score_query
from core.db_manager import load_db

llm_bp = Blueprint('llm', __name__)

@llm_bp.route('/llm/analyze_error', methods=['POST'])
def analyze_error(): ...
# ... 其余 8 条路由
```

### B.3 依赖关系图

```
app.py
  ├── core/session_store.py  ← _store 管理（双向：app + llm_routes 均 import）
  ├── core/llm_client.py     ← init + is_configured
  ├── core/llm_routes.py (Blueprint)
  │     ├── core/session_store.py  ← get_results
  │     ├── core/llm_client.py     ← call_llm
  │     ├── core/matcher.py        ← score_query（P3 预筛选候选）
  │     └── core/db_manager.py     ← load_db（P3/P5/P6）
  ├── core/log_parser.py     ← 不变
  ├── core/matcher.py        ← 不变（新增 score_query）
  ├── core/db_manager.py     ← 不变
  └── core/reporter.py       ← 不变
```

### B.4 前端 JS 架构

**位置**：Inline，写在模板末尾的 `{% if llm_enabled %}<script>...</script>{% endif %}` 块内，与现有 `writeBack()` 风格一致，不引入外部 JS 文件。

**数据传递**：通过 onclick 参数传入 Jinja2 数据：
```html
<button class="btn-ai"
  onclick="aiAnalyze({{ loop.index0 }}, '{{ e.level|e }}', '{{ e.error_id|e }}', '{{ e.location|e }}')">
  🤖 AI 分析
</button>
```

**Loading 状态模式**（复用现有 `runQuery()` 风格）：
```javascript
async function aiAnalyze(idx, level, errorId, location) {
  const btn = document.getElementById(`aiBtn_${idx}`);
  btn.textContent = '分析中...'; btn.disabled = true;
  try {
    const resp = await fetch('/llm/analyze_error', { method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({level, error_id:errorId, location, description:...})
    });
    const data = await resp.json();
    if (data.ok) { /* 预填字段 */ } else { /* 静默恢复 */ }
  } finally {
    btn.textContent = '🤖 AI 分析'; btn.disabled = false;
  }
}
```

**模态框（P2.6、P4）**：预渲染 HTML，CSS class 控制显隐：
```html
<div id="aiPatternModal" class="ai-modal" style="display:none">
  <div class="ai-modal-backdrop" onclick="closeAiModal('aiPatternModal')"></div>
  <div class="ai-modal-dialog">
    <div class="ai-modal-header">AI 模式分析 <button onclick="closeAiModal(...)">✕</button></div>
    <div class="ai-modal-body" id="aiPatternContent"><!-- 动态插入 --></div>
  </div>
</div>
```

**P2.5 DOM 重排**（`container.appendChild`，按 ranked 顺序移动节点）：
```javascript
const container = document.getElementById(`entriesContainer_${errIdx}`);
const nodes = Array.from(container.querySelectorAll('.entry-item'));
data.ranked.forEach((origIdx, newPos) => {
  nodes[origIdx].querySelector('.ai-reason-tag').textContent = data.reasons[newPos];
  container.appendChild(nodes[origIdx]);  // 移动到末尾 = 按顺序排列
});
```

**P5 串行调用**（用户一键，前端自动链式调用）：
```javascript
async function semanticSearch() {
  const candidates = await runQueryGetCandidates();  // 先执行规则查询
  if (!candidates || candidates.length <= 1) {
    showToast('规则查询无结果，请调整搜索条件'); return;
  }
  const resp = await fetch('/llm/semantic_query', { body: JSON.stringify({candidates, ...}) });
  const data = await resp.json();
  if (data.ok) reorderQrCards(data.ranked, data.reasons);
}
```

**XSS 防护**：复用现有 `esc()` 函数对所有 LLM 返回内容转义后再插入 DOM。

---

## 三、LLM 嵌入点设计

按用户价值排序，优先级从高到低。

### P0 - 架构基础设施（必须最先实现）

**功能**：LLM 客户端和配置管理
**位置**：新增 `core/llm_client.py`
**影响**：所有后续功能的基础

---

### P1 - 未匹配错误自动分析（最高价值）

**痛点**：每条未匹配需手工填写 5 个字段，耗时 2~5 分钟
**位置**：`templates/result.html` → `match-box.unmatched` 区块

#### 交互设计
```
未匹配错误卡片
├── [❌ 未匹配]
├── [🤖 AI 分析] ← {% if llm_enabled %}
│   └── 点击后：
│       → 显示 loading 状态
│       → POST /llm/analyze_error
│       → 成功：预填 5 个字段，显示「AI 建议（请审核）」提示条（浅黄色）
│       → 空字段（LLM 返回 ""）不预填，保持原始空白
│       → 失败：按钮恢复可用，表单保持空白（静默降级）
└── [手动填写回写表单...]
```

#### 新增路由
```
POST /llm/analyze_error
请求：{ level, error_id, location, description }
响应：
  成功：{ ok:true, keywords, reason, category, solution, module }
  失败：{ ok:false, reason:"..." }
```

#### Prompt 设计
```
System: 你是一名经验丰富的芯片验证工程师，专注于 UVM 仿真日志分析。
        请严格以 JSON 格式回答，不包含任何其他文字。
        如果某字段信息不足，请返回空字符串 ""，不要编造内容。

User:   分析以下 UVM 仿真错误，返回如下 JSON 对象：
        {
          "keywords": "3-5个关键词，中英文均可，英文逗号分隔，提取最能定位此错误的技术词汇",
          "reason":   "根本原因说明，信息不足时返回空字符串",
          "category": "从以下选项选一个：
                       DUT Bug（硬件设计本身存在缺陷）|
                       TB Bug（测试平台/UVM 组件代码错误）|
                       用例问题（测试用例配置或激励设计错误）|
                       工具问题（仿真器/编译环境/工具链错误）|
                       其他问题（无法归入以上分类）",
          "solution": "建议解决方案，信息不足时返回空字符串",
          "module":   "从 location 字段推断的出错模块名，无法判断时返回空字符串"
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
# 空字段（""）不预填，保留原始空白
# category 模糊匹配枚举，无匹配则默认"其他问题"
```

---

### P2.5 - 多条匹配智能推荐

**痛点**：当错误命中多条 KB 条目时，按"录入日期降序"可能不是最相关的
**位置**：`templates/result.html` → 已匹配错误卡片（entries.length > 1 时显示）

#### 交互设计
```
已匹配错误卡片（entries.length > 1 时）：
├── [共 N 条] [🤖 智能推荐 ▸] ← {% if llm_enabled and entries|length > 1 %}
│
│   默认：按录入日期降序（服务端现有行为，不变）
│   点击"智能推荐"后：
│       → 显示 loading 状态
│       → POST /llm/rank_entries
│       → 成功：按 LLM 评分重新排列条目，每条显示推荐理由标签
│       → 按钮变为「✅ 已推荐」 + [恢复默认] 按钮
│       → 失败：静默保持原顺序
└── 推荐条目高亮（绿色边框 .ai-ranked-entry）
```

#### 新增路由
```
POST /llm/rank_entries
请求：{ entries:[{...}], current_error:{level, error_id, location, description} }
响应：
  成功：{ ok:true, ranked:[0,2,1,...], reasons:["推荐理由","...",...] }
  失败：{ ok:false, reason:"..." }
```

#### Prompt 设计
```
System: 你是一名经验丰富的芯片验证工程师。

User:   根据以下当前错误，对候选知识库条目按相关性从高到低排序，并说明每条推荐理由。

        当前错误：
        级别：{level} | 错误ID：{error_id}
        位置：{location}
        描述：{description[:500]}

        候选条目（共{N}条）：
        [0] ID:{错误ID} | 模块:{所属模块} | 原因:{报错原因[:80]} | 关键词:{关键描述关键词}
        [1] ...

        返回JSON：
        {
          "ranked": [2, 0, 1, ...],
          "reasons": ["条目[2]推荐原因（≤30字）", "条目[0]推荐原因", ...]
        }

temperature=0.2, max_tokens=500
```

#### 实现要点
- 服务端保持日期降序不变；前端收集 entries + 当前错误后调用 LLM
- LLM 失败时静默保持原顺序
- `reasons` 顺序与 `ranked` 对应，展示在对应条目卡片上

---

### P2.6 - 自定义提取功能

**痛点**：需要从大文件中按自然语言描述或行号范围提取特定内容
**位置**：`templates/result.html` → 顶部 `.top-actions` 按钮区
**限制**：**仅 Path 模式 + 单文件分析时显示**（Upload 模式文件已删除）

#### 交互设计
```
顶部栏（Path 模式 + 单文件时）：
[← 重新上传] [⬇ Excel] [⬇ HTML]  [📋 自定义提取] ← {% if llm_enabled and is_path_mode and is_single_file %}

点击"自定义提取"：
→ 弹出模态框，包含单个表单：

  自然语言描述：[用户输入查询，如"找 DMA 相关错误"]（必填）
  行号范围：    [起始] ~ [结束]  （可选）
  UVM 级别过滤：○ ALL  ○ FATAL  ○ ERROR  ○ WARNING  （可选）
  [开始提取]

→ 后端读取 _store[sid]['file_paths'][0]
→ LLM 返回内容：
  - 含合法 JSON → 渲染为表格 + [导出] 按钮
  - 否则 → 显示纯文本 + [复制] 按钮
```

#### 新增路由
```
POST /llm/custom_extract
请求：{ query:str, line_start:int|null, line_end:int|null, level_filter:str|null }
  - file_path 不传，后端从 _store[sid]['file_paths'][0] 取
响应：
  成功：{ ok:true, format:"json"|"text", data:..., lines_processed:int }
  失败：{ ok:false, reason:"..." }
```

#### 数据源与采样策略
- **Path 模式分析时**，`app.py` 将原始文件路径列表存入 `_store[sid]['file_paths']`
- 有行号范围：只读该范围（上限 3000 行）
- 无行号范围：均匀采样 1000 行（含行号注释）
- `level_filter` 在 Python 侧预过滤后再送 LLM

#### 输出自动判断（后端）
```python
m = re.search(r'\{.*\}', llm_response, re.DOTALL)
if m:
    try:
        json.loads(m.group())
        return {"ok": True, "format": "json", "data": m.group(), ...}
    except Exception:
        pass
return {"ok": True, "format": "text", "data": llm_response, ...}
```

---

### P3 - 相似错误推荐（写回辅助）

**痛点**：规则匹配为空，但 KB 中可能存在同根因但 ID/关键词不同的条目
**位置**：`templates/result.html` → 未匹配回写表单内，折叠面板

#### 交互设计
```
回写表单内：
└── [🔍 查找相似已知错误 ▸] ← {% if llm_enabled %}
    展开后：
        → 自动触发 POST /llm/similar_errors
        → 显示最多 5 条相似条目卡片
        → 每张卡片：错误ID + 报错原因 + 解决方案 + 相似原因
        → [参考此条目] 按钮：将以下内容复制到表单：
          - 报错原因 → 报错原因字段
          - 解决方案 → 解决方案字段
          - 关键描述关键词 → 关键描述关键词字段
```

#### 新增路由
```
POST /llm/similar_errors
请求：{ db_path, level, error_id, description, top_k=5 }
响应：
  成功：{ ok:true, similar:[{_row_idx, 错误ID, 报错原因, 解决方案, 关键描述关键词, similarity_reason}] }
  失败：{ ok:false, similar:[] }
```

#### Prompt 设计
```
System: 你是一名经验丰富的芯片验证工程师。

User:   以下是一个尚未匹配的 UVM 错误：
        级别：{level} | 错误ID：{error_id}
        描述：{description[:500]}

        以下是知识库候选条目（共{N}条，已按关键词相关度预筛选）：
        [0] ID:{错误ID} | 原因:{报错原因[:80]} | 方案:{解决方案[:60]} | 关键词:{关键描述关键词}
        [1] ...

        请找出与当前错误根因相同或高度相似的条目（最多5条）。
        无相似条目时返回空列表。

        返回JSON：{"similar": [{"idx": 0, "reason": "≤30字相似原因"}]}

temperature=0.2, max_tokens=400
```

#### 实现要点
- **先用 `/query` 的 token 重叠打分逻辑筛选 top-50 候选**（不是直接取前50行）
- 复用 `app.py` 里现有的 query 打分函数，传入 description 作为查询文本
- LLM 返回的 `idx` 映射回候选列表中实际条目的 `_row_idx`

---

### P_摘要 - 错误描述智能摘要

**痛点**：多行续行描述含大量十六进制转储/路径，扫描效率低
**位置**：`templates/result.html` → 每条错误的描述行末尾

#### 交互设计
```
错误描述行：
├── [完整描述文本...]
└── [📝 AI 摘要] ← {% if llm_enabled %}
    点击后：
        → 原描述折叠（display:none）
        → 蓝色徽章（.ai-summary）显示 20 字以内摘要
        → 按钮变为"展开" → 点击恢复原描述
```

#### 新增路由
```
POST /llm/summarize
请求：{ level, error_id, description }  ← description 截断至 1000 字符
响应：
  成功：{ ok:true, summary:"≤20字摘要" }
  失败：{ ok:false }
```

#### Prompt
```
用一句话（20字以内）总结以下 UVM {level} 错误 {error_id} 的核心问题：
{description}

temperature=0.1, max_tokens=60
```

---

### P4 - 批量错误模式分析

**痛点**：回归测试数十个 log 失败时，需人工归纳主要失败模式
**位置**：`templates/result.html` → 顶部 `.top-actions` 按钮区
**限制**：**仅多文件分析时显示**

#### 交互设计
```
顶部栏（多文件时）：
[← 重新上传] [⬇ Excel] [⬇ HTML]  [🤖 AI 模式分析] ← {% if llm_enabled and is_multi_file %}

点击"AI 模式分析"：
→ 弹出模态框，显示 spinner
→ POST /llm/batch_patterns
→ 成功：展示 3~7 个模式卡片（.ai-pattern-card）：
        标题 + 涉及错误ID列表 + 影响文件数 + 模式特征 + 建议操作
→ 失败：模态框内显示错误信息
```

#### 新增路由
```
POST /llm/batch_patterns
请求：{}  （从 Flask session 读取 _store[sid]）
响应：
  成功：{ ok:true, total_files:N,
          patterns:[{title, error_ids:[], file_count:N, description, suggested_action}] }
  失败：{ ok:false, reason:"..." }
```

#### 数据聚合逻辑
```python
# 遍历所有文件的 top_errors，按 (level, error_id) 去重
seen = {}  # key: (level, error_id) → {file_count, description(首次出现)}
for file_result in results:
    for err in file_result['top_errors']:
        key = (err['level'], err['error_id'])
        if key not in seen:
            seen[key] = {'file_count': 0, 'description': err['description']}
        seen[key]['file_count'] += 1
# 按 file_count 降序取前 20 条
top20 = sorted(seen.items(), key=lambda x: -x[1]['file_count'])[:20]
```

#### Prompt 设计
```
System: 你是一名日志分析专家，擅长归纳批量测试失败的根本原因模式。
        请严格以 JSON 格式回答。

User:   以下是一批回归测试的失败错误统计（已去重，按出现文件数降序）：

        [1] 级别:{level} | ID:{error_id} | 出现{file_count}个文件 | 描述:{description[:150]}
        [2] ...

        请归纳 3~7 个主要失败模式，返回JSON：
        {
          "patterns": [
            {
              "title": "一句话模式标题",
              "error_ids": ["ID1", "ID2"],
              "file_count": 15,
              "description": "模式特征说明",
              "suggested_action": "建议排查方向"
            }
          ]
        }

temperature=0.3, max_tokens=800
```

---

### P5 - 语义知识库查询增强

**痛点**：现有 `/query` token 重叠算法无法识别同义词和中文近义表达
**位置**：`templates/index.html` → 查询 Tab 的按钮区

#### 交互设计
```
查询 Tab 按钮区：[查询]  [🔍 语义搜索] ← {% if llm_enabled %}

一步操作（用户只需点一次）：
1. 前端自动先调 /query 获取规则匹配候选（最多 30 条）
2. 若候选 ≤1 条：toast 提示"规则查询无结果，请调整搜索条件"，不调 LLM
3. 候选 >1 条：将候选列表 + 查询文本发给 /llm/semantic_query 重排序
4. 前端按返回顺序重新排列 .qr-card 卡片，每张卡片下方显示相关性说明
```

#### 新增路由
```
POST /llm/semantic_query
请求：{ db_path, level, text,
        candidates:[{_row_idx, 错误ID, 错误类型, 报错原因, 关键描述关键词}] }
响应：
  成功：{ ok:true, ranked:[int,...], reasons:["相关性说明",...] }
  失败：{ ok:false }
```

#### Prompt 设计
```
System: 你是一名知识库搜索专家。请根据用户查询文本，对候选结果按语义相关性重新排序。
        仅返回JSON，不包含其他文字。

User:   用户查询：{text}

        候选结果（共{N}条，已按关键词初步筛选）：
        [0] ID:{错误ID} | 级别:{错误类型} | 原因:{报错原因[:80]} | 关键词:{关键描述关键词}
        [1] ...

        按语义相关性从高到低排序，并说明每条的相关性原因（≤20字）：
        {
          "ranked": [2, 0, 1, ...],
          "reasons": ["与ranked顺序对应的原因说明", ...]
        }

temperature=0.1, max_tokens=300
```

---

### P6 - 知识库语义去重质量检查

**痛点**：规则去重基于精确字符串，无法识别措辞不同的同义条目
**位置**：`templates/index.html` → "添加条目" Tab 底部

#### 交互设计
```
"添加条目" Tab 底部：
└── [🔍 知识库质量检查] ← {% if llm_enabled %}

点击后：
→ POST /llm/kb_review
→ 返回疑似重复对列表（表格形式）：
   | 条目A（错误ID+原因） | 条目B（错误ID+原因） | 相似原因 | [保留两者] [删除A] [删除B] |
→ 操作按钮调用现有 /kb/delete 端点
```

#### 新增路由
```
POST /llm/kb_review
请求：{ db_path, max_check:200 }
响应：
  成功：{ ok:true, suspect_pairs:[{row_a, row_b, similarity_reason}] }
        row_a/row_b 字段：{_row_idx, 错误类型, 错误ID, 报错原因, 解决方案}
  失败：{ ok:false, reason:"..." }
```

#### Prompt 设计
```
System: 你是一名知识库维护专家。请找出以下条目中描述同一根因的重复对。
        仅返回JSON，无重复时返回 {"pairs": []}。

User:   错误类型「{level}」的知识库条目（共{N}条）：

        [0] row:{_row_idx} | ID:{错误ID} | 原因:{报错原因[:80]} | 方案:{解决方案[:60]}
        [1] ...

        返回：{"pairs": [{"a":0, "b":3, "reason":"≤30字相似原因"}]}
        （a, b 为上方列表索引，不是 row 号）

temperature=0.1, max_tokens=500
```

#### 分批策略（滑动窗口）
- 按 `错误类型` 分组
- 每组使用**滑动窗口（窗口=20条，步长=10）**，确保任意相邻 20 位置内的条目都会被比较
- 同一组 N 条约需 `ceil((N-10)/10)` 次 LLM 调用
- `max_check=200` 限制每组最多检查 200 条
- LLM 返回的 `a/b` 是批次内索引，需映射回实际 `_row_idx`
- 跨批次相同 pair 去重：用 `frozenset({row_a, row_b})` 作为 key

---

## 四、文件变更清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `core/session_store.py` | 新增 | `_store` + `get/set_results`（从 app.py 迁移，解决 Blueprint 循环依赖） |
| `core/llm_client.py` | 新增 | LLM API 客户端，无 Flask 依赖（约 120 行） |
| `core/llm_routes.py` | 新增 | Flask Blueprint，9 条 LLM 路由（约 350 行） |
| `core/matcher.py` | 修改 | 新增 `score_query()` 函数（从 `/query` 路由提取，P3 共用） |
| `app.py` | 修改 | 5~6 处最小改动：session_store 导入、Blueprint 注册、llm_enabled 注入、file_paths 存储、score_query 替换 |
| `templates/result.html` | 修改 | P1/P2.5/P2.6/P3/P_摘要/P4 按钮 + JS（inline，`{% if llm_enabled %}` 块） |
| `templates/index.html` | 修改 | P5/P6 按钮 + JS（inline，`{% if llm_enabled %}` 块） |
| `static/style.css` | 修改 | 新增 AI 相关 CSS 类 |

### 新增 CSS 类
| 类名 | 用途 |
|------|------|
| `.ai-summary` | P_摘要 描述摘要徽章（蓝色） |
| `.ai-hint` | P1 AI建议提示条（浅黄色） |
| `.ai-suggest-card` | P3 相似错误卡片 |
| `.ai-similar-section` | P3 折叠容器 |
| `.ai-pattern-card` | P4 批量模式分析卡片 |
| `.ai-pattern-modal` | P4 模态框 |
| `.ai-rank-btn` | P2.5 智能推荐按钮 |
| `.ai-ranked-entry` | P2.5 推荐条目高亮（绿色边框） |
| `.ai-extract-modal` | P2.6 自定义提取模态框 |
| `.btn-ai`, `.btn-ai-sm` | AI 操作按钮（带特色样式） |

### 新增路由汇总（共 9 条）
| 路由 | 对应功能 |
|------|---------|
| `POST /llm/analyze_error` | P1 未匹配自动分析 |
| `POST /llm/rank_entries` | P2.5 多条匹配智能推荐 |
| `POST /llm/custom_extract` | P2.6 自定义提取 |
| `POST /llm/similar_errors` | P3 相似错误推荐 |
| `POST /llm/summarize` | P_摘要 描述一句话摘要 |
| `POST /llm/batch_patterns` | P4 批量模式分析 |
| `POST /llm/semantic_query` | P5 语义知识库查询 |
| `POST /llm/kb_review` | P6 知识库语义去重 |

---

## 五、降级与安全策略

| 场景 | 行为 |
|------|------|
| `llm_config.json` 不存在 | `is_configured()=False`，Jinja 不渲染任何 AI 按钮，功能与现在完全一致 |
| LLM 接口超时 | 路由返回 `{ok:false}`，前端按钮恢复可用，不影响手工流程 |
| LLM 返回非 JSON | 正则提取 `{...}` 块，失败则返回 `{ok:false}` |
| LLM 返回非法 category（P1） | 从枚举中模糊匹配，无匹配则默认「其他问题」 |
| KB 为空（P3/P6） | 直接返回 `{ok:true, similar:[]}` 或 `{ok:true, suspect_pairs:[]}` |
| entries.length <= 1（P2.5） | 不显示「智能推荐」按钮 |
| Upload 模式（P2.6） | 不显示「自定义提取」按钮 |
| 多文件模式（P2.6） | 不显示「自定义提取」按钮 |
| 单文件模式（P4） | 不显示「AI 模式分析」按钮 |
| 语义查询候选 ≤1（P5） | toast 提示，不调 LLM |

---

## 六、实施顺序

| 步骤 | 内容 | 依赖 |
|------|------|------|
| 1 | `core/session_store.py` — 迁移 `_store`（同步修改 app.py 导入） | 无 |
| 2 | `core/matcher.py` — 提取 `score_query()` 函数 | 无 |
| 3 | `core/llm_client.py` — LLM API 客户端 | 无 |
| 4 | `core/llm_routes.py` — Blueprint + 9 条路由 | 步骤 1/2/3 |
| 5 | `app.py` — 注册 Blueprint + init + Jinja 全局 + file_paths | 步骤 1/3/4 |
| 6 | `templates/result.html` — P1/P2.5/P2.6/P3/P_摘要/P4 | 步骤 4/5 |
| 7 | `templates/index.html` — P5/P6 | 步骤 4/5 |
| 8 | `static/style.css` — AI 相关 CSS 类 | 步骤 6/7 |

步骤 1~3 无互相依赖，可并行实施。

---

## 七、验证要点

1. **基础版验证**：无 `llm_config.json` 时，启动工具，所有页面不显示 `AI` 字样按钮
2. **P1 验证**：有效配置时，未匹配错误一键分析，5 个字段正确预填（空字段不预填）
3. **P2.5 验证**：命中多条时，智能推荐重排顺序，每条显示推荐理由
4. **P2.6 验证**：Path 模式单文件时显示按钮，Upload 模式不显示
5. **降级验证**：LLM 超时/失败时，不影响现有手工流程
6. **回归验证**：现有 `test_all_features.py` 全部通过（9条新路由不影响现有路由）
