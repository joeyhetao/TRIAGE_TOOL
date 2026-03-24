# triage_tool — LLM 嵌入点设计方案 v1.0

> **声明**：本文档为纯设计方案，不含任何代码修改指令。

---

## 一、背景与目标

triage_tool 当前匹配引擎完全基于规则：精确 `error_id` 匹配 + 关键词 AND 子串匹配。工程师在面对"未匹配"错误时，需要手动填写根因分类、解决方案等字段，平均耗时 2~5 分钟/条，且依赖个人经验积累。

**目标**：将 LLM 作为**可选增强层**叠加在现有规则之上，满足：

- LLM 未配置时，工具行为与现在 100% 一致（`llm_enabled = False`，所有 AI 按钮不渲染）
- 不引入任何新的第三方安装包（HTTP 调用复用 Flask 已有的 `requests`，或降级到标准库 `urllib`）
- 配置文件（`llm_config.json`）放在 `BASE_DIR`（exe 同目录），不打包进 exe，便于部署修改
- 支持任意 OpenAI-compatible 接口（Qwen、DeepSeek、GPT 等内网部署均可）

---

## 二、基础架构：`core/llm_client.py`（新增文件）

所有 LLM 能力的**唯一入口**，其他文件只 import 此模块。

### 2.1 配置文件格式

```json
// BASE_DIR/llm_config.json
{
  "endpoint": "http://your-llm-server/v1/chat/completions",
  "api_key": "sk-xxx",
  "model": "qwen2.5-7b",
  "timeout": 30
}
```

配置优先级：**环境变量 > 文件**

| 环境变量 | 对应字段 |
|---|---|
| `LLM_ENDPOINT` | endpoint |
| `LLM_API_KEY` | api_key |
| `LLM_MODEL` | model |
| `LLM_TIMEOUT` | timeout（整数秒） |

### 2.2 公共接口

```
init(base_dir: Path) → None
    由 app.py 在确定 BASE_DIR 后调用一次，加载并缓存配置

is_configured() → bool
    快速检查 endpoint + model 是否均已设置
    注入到 Jinja2 全局：llm_enabled = is_configured()
    模板中用 {% if llm_enabled %} 包裹所有 AI 按钮

call_llm(prompt, system=None, max_tokens=500, temperature=0.2) → str
    调用 /chat/completions（OpenAI 格式）
    失败/未配置时返回 ""，不向上抛异常
    优先用 requests，降级用 urllib
```

### 2.3 app.py 的 3 处改动

```
1. import：from core import llm_client
2. 初始化（UPLOAD_DIR.mkdir 之后）：llm_client.init(BASE_DIR)
3. Jinja 全局（urlencode filter 之后）：
   app.jinja_env.globals['llm_enabled'] = llm_client.is_configured()
```

---

## 三、六个嵌入点

按**用户价值**排序，P1 最高。

---

### P1 — 未匹配错误自动分析（最高价值）

**痛点**：每条未匹配错误需手动填写 5 个字段。
**位置**：`result.html` → `match-box.unmatched` 块

#### 新增路由

```
POST /llm/analyze_error
请求：{ level, error_id, location, description }
响应：
  成功：{ ok:true, keywords, reason, category, solution, module }
  失败：{ ok:false, reason:"..." }
  category 枚举：DUT Bug | TB Bug | 用例问题 | 工具问题 | 其他问题
```

#### 交互流程

```
未匹配错误块顶部
├── [❌ 未匹配 — 建议将该报错条目添加至错误数据库]
└── [🤖 AI 分析] ← {% if llm_enabled %} 渲染此按钮

点击"AI 分析"
→ 显示 spinner
→ POST /llm/analyze_error（传入 level/error_id/location/description）
→ 成功：预填 关键描述关键词/报错原因/根因分类/解决方案/所属模块
         顶部显示提示条「AI 建议（请审核后确认）」
         按钮变为「✅ 已分析」
→ 失败：按钮恢复，表单保持空白（静默降级）
```

#### Prompt 策略

```
System: 你是一名经验丰富的芯片验证工程师，擅长分析 UVM 仿真日志中的错误。
        请严格以 JSON 格式回答，不要包含其他文字。

User:   分析以下 UVM 仿真错误，返回 JSON 对象：
        {
          "keywords": "3-5个关键词，英文逗号分隔",
          "reason":   "根本原因说明",
          "category": "DUT Bug / TB Bug / 用例问题 / 工具问题 / 其他问题（选其一）",
          "solution": "建议解决方案",
          "module":   "出错模块名"
        }

temperature=0.2, max_tokens=400
```

#### 解析容错

```python
m = re.search(r'\{.*\}', content, re.DOTALL)
data = json.loads(m.group()) if m else {}
```

---

### P2 — 相似错误推荐（写回辅助）

**痛点**：规则匹配为空，但 KB 中可能存在同根因但 ID/关键词不同的条目。
**位置**：`result.html` → 未匹配回写表单内，折叠面板

#### 新增路由

```
POST /llm/similar_errors
请求：{ db_path, level, error_id, description, top_k=5 }
响应：
  成功：{ ok:true, similar:[{_row_idx, 错误ID, 报错原因, 解决方案, similarity_reason}] }
  失败：{ ok:false, similar:[] }
```

#### 交互流程

```
回写表单内（写回按钮下方）
└── [🔍 查找相似已知错误 ▸] ← {% if llm_enabled %}

展开后：
→ 自动触发 POST /llm/similar_errors
→ 显示最多 5 条相似条目卡片：
   - 错误ID + 报错原因 + 解决方案
   - 相似原因说明（来自 LLM）
   - [参考此条目] 按钮：将报错原因/解决方案复制到表单
```

#### 实现要点

- 从 KB 按 `level` 筛选后取前 50 条
- 压缩为 `[i] ID:xxx | KW:xxx | 原因:前60字` 格式发给 LLM
- LLM 返回 `{"similar": [{"idx":0, "reason":"..."}]}`
- 容错同 P1

---

### P3 — 错误描述一句话摘要

**痛点**：多行续行描述含大量十六进制转储/路径，扫描慢。
**位置**：`result.html` → 每条错误的描述行末尾

#### 新增路由

```
POST /llm/summarize
请求：{ level, error_id, description }  ← description 截断至 1000 字符
响应：
  成功：{ ok:true, summary:"≤20字摘要" }
  失败：{ ok:false }
```

#### 交互流程

```
描述行：[完整描述文本]  [AI摘要] ← {% if llm_enabled %}

点击"AI摘要"：
→ 原描述折叠（display:none）
→ 蓝色徽章显示摘要文字
→ 按钮变为"展开" → 点击恢复原描述
```

#### Prompt

```
用一句话（20字以内）总结以下 UVM {level} 错误 {error_id} 的核心问题：
{description}

temperature=0.1, max_tokens=60
```

---

### P4 — 批量错误模式分析

**痛点**：回归测试数十个 log 失败时，需人工归纳主要失败模式。
**位置**：`result.html` → 顶部 `.top-actions` 按钮区

#### 新增路由

```
POST /llm/batch_patterns
请求：{}  （从 Flask session 读取 _store[sid].all_errors）
响应：
  成功：{ ok:true, total_files:N,
          patterns:[{title, error_ids:[], summary, suggested_action}] }
  失败：{ ok:false, reason:"..." }
```

#### 交互流程

```
顶部栏：[← 重新上传] [⬇ Excel] [⬇ HTML]  [🤖 AI 模式分析] ← {% if llm_enabled %}

点击"AI 模式分析"：
→ 弹出模态框，显示 spinner
→ POST /llm/batch_patterns
→ 成功：展示 3~7 个模式卡片（标题 + 涉及错误ID + 描述 + 建议操作）
→ 失败：模态框内显示错误信息
```

#### 实现要点

- 聚合所有文件的 `all_errors`，按 `(level, error_id)` 去重并统计文件出现次数
- 取出现次数最多的前 20 条发给 LLM
- `temperature=0.3, max_tokens=600`

---

### P5 — 语义知识库查询

**痛点**：现有 `/query` token 重叠算法无法识别同义词和中文近义表达。
**位置**：`index.html` → 查询 Tab 的按钮区，紧邻"查询"按钮

#### 新增路由

```
POST /llm/semantic_query
请求：{ db_path, level, text,
        candidates:[{_row_idx, 错误ID, 报错原因, 关键描述关键词}] }
响应：
  成功：{ ok:true, ranked_row_idxs:[int,...] }
  失败：{ ok:false }
```

#### 交互流程

```
查询 Tab 按钮区：[查询]  [语义搜索] ← {% if llm_enabled %}

用法（两步）：
1. 先点"查询"获取规则匹配候选（最多 30 条）
2. 点"语义搜索"将候选列表 + 查询文本发给 LLM 重排序
3. 前端 JS 按返回顺序重新排列已渲染的 .qr-card 卡片

（若未先查询，点"语义搜索"自动先调用 runQuery()）
```

#### 实现要点

- `temperature=0.1, max_tokens=200`
- LLM 返回 `{"ranked": [2, 0, 1, ...]}` — 原候选的索引顺序
- 前端按 `ranked_row_idxs` 重新排列 DOM 节点

---

### P6 — 知识库语义去重质量检查

**痛点**：规则去重基于精确字符串，无法识别措辞不同的同义条目。
**位置**：`index.html` → "添加条目" Tab 底部，折叠区

#### 新增路由

```
POST /llm/kb_review
请求：{ db_path, max_check:200 }
响应：
  成功：{ ok:true, suspect_pairs:[{row_a, row_b, similarity_reason}] }
        row_a/row_b 字段：{_row_idx, 错误类型, 错误ID, 报错原因, 解决方案}
  失败：{ ok:false, reason:"..." }
```

#### 交互流程

```
"添加条目" Tab 底部：
└── [🔍 知识库质量检查] ← {% if llm_enabled %}

点击后：
→ POST /llm/kb_review
→ 返回疑似重复对列表（表格形式）：
   | 条目A | 条目B | 相似原因 | [保留两者] [删除A] [删除B] |
→ 操作按钮调用现有 /kb/delete 端点
```

#### 实现要点

- 按 `错误类型` 分组，每组以 20 条为批次分批发给 LLM
- LLM 返回 `{"pairs": [{"a":0, "b":1, "reason":"..."}]}`
- `temperature=0.1`（最高一致性）

---

## 四、实施顺序建议

| 阶段 | 内容 | 说明 |
|------|------|------|
| 阶段一 | `core/llm_client.py` | 所有后续功能的基础设施 |
| 阶段一 | `app.py` 3处改动 | init + llm_enabled 全局 + 路由注册 |
| 阶段一 | P1 + P3 | 最高价值 + 最小范围，快速验证 prompt 策略 |
| 阶段二 | P2 + P5 | 写回辅助 + 查询增强 |
| 阶段三 | P4 + P6 | 批量分析 + 维护工具 |

---

## 五、涉及文件清单

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `core/llm_client.py` | 新增 | LLM 客户端全部逻辑 |
| `app.py` | 修改 | import + init + llm_enabled + 6条路由 |
| `templates/result.html` | 修改 | P1/P2/P3/P4 按钮 + JS |
| `templates/index.html` | 修改 | P5/P6 按钮 + JS |
| `static/style.css` | 修改 | 新增 AI 相关 CSS 类 |

### 新增 CSS 类

| 类名 | 用途 |
|------|------|
| `.ai-summary` | P3 描述摘要徽章（蓝色） |
| `.ai-hint` | P1 AI建议提示条（浅黄色） |
| `.ai-suggest-card` | P2 相似错误卡片 |
| `.ai-similar-section` | P2 折叠容器 |
| `.ai-pattern-card` | P4 模式分析卡片 |
| `.ai-pattern-modal` | P4 模态框 |
| `.btn-ai`, `.btn-ai-sm` | AI 操作按钮（带特色样式） |

---

## 六、降级与安全策略

| 场景 | 行为 |
|------|------|
| `llm_config.json` 不存在 | `is_configured()=False`，Jinja 不渲染任何 AI 按钮，功能与现在完全一致 |
| LLM 接口超时 | 路由返回 `{ok:false}`，前端按钮恢复可用，表单保持空白 |
| LLM 返回非 JSON | 正则提取 `{...}` 块，失败则返回 `{ok:false}` |
| LLM 返回非法 category | 从枚举中模糊匹配，无匹配则默认「其他问题」 |
| KB 为空（P2/P6） | 直接返回 `{ok:true, similar:[]}` |

---

## 七、验证要点

1. 无 `llm_config.json` 时：启动工具，result.html 不含任何 `AI` 字样按钮
2. 有效配置时：`/llm/analyze_error` 返回 JSON，前端正确预填 5 个字段
3. LLM 超时（timeout=1秒）时：按钮恢复，不影响手动填写和写回流程
4. 现有 `test_all_features.py` 全部通过（6条新路由不影响现有路由）
