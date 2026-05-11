# 仿真日志分类分诊工具 — 产品需求文档（PRD）

**文档版本**：v2.3
**基准代码版本**：2026-05-11
**适用范围**：功能增改、需求评审、开发参考

---

## 1. 产品定位

面向芯片验证工程师的内网桌面工具，用于批量解析 UVM 仿真日志、自动匹配已知错误知识库、沉淀和复用排查经验，减少重复定位时间。

**部署形态**：
- **Windows**：单个 `triage_tool.exe`，双击运行，启动后自动打开浏览器访问本地 Web 界面（http://127.0.0.1:5000）
- **Linux**：`python app.py` 直接启动，适用于在仿真服务器上就地分析本地日志

知识库文件（`error_db.xlsx`）支持放置于网络共享盘供多人共用。

---

## 2. 用户与使用场景

| 角色 | 使用场景 |
|---|---|
| 验证工程师 | 上传或指定本地 log，查看自动分析结果，快速定位前几条错误 |
| 资深工程师 | 将新错误根因写回知识库；对命中条目补充录入新根因；查询知识库积累经验 |
| 团队负责人 | 导出分析报告，归档或发送给相关人员 |

---

## 3. 当前功能（v2.2）

### 3.1 日志输入

支持三种模式，通过首页 Tab 切换：

**模式一：上传文件**
- 拖拽或点击选择 `.log` 文件，可多选批量上传
- 文件通过浏览器 HTTP multipart 上传至服务器 `uploads/` 目录后解析
- 单文件大小限制：10 GB

**模式二：指定路径**（v1.2 新增）
- 在文本框中直接填写服务器本地文件路径，无需上传拷贝
- 支持 glob 通配符（`*`、`**` 递归），每行一条路径或逗号分隔
- 服务端展开 glob、过滤非 `.log` 文件，单次最多 **5000 个文件**
- 适用于日志在仿真服务器本地的场景，省去大文件传输开销
- 典型 glob 示例：`regr/**/test/tc_*.log`（仅扫描 test 子目录下的 tc_ 开头日志）

**模式三：查询知识库**（v1.3 新增，见 3.8）

**模式四：添加条目**（v1.5 新增，见 3.9）

**模式五：解析配置**（v1.8 新增，见 3.15/3.16）

### 3.2 日志解析

**UVM 日志格式**：
```
UVM_ERROR /path/file.sv(142) @ 1000ns: uvm_test_top.env [ID] message
```

- 解析每个日志文件，提取所有 `UVM_FATAL` / `UVM_ERROR` / `UVM_WARNING` 以及**额外错误关键词**（见 3.15）条目
- **`UVM_WARNING` 仅统计计数，不进入 `top_errors` 列表，不参与知识库匹配**（v1.3 变更）
- **额外错误关键词**：匹配 `^关键词: 描述内容` 格式（行首+冒号），与 UVM_ERROR/FATAL 等价，进入 top_errors 并参与知识库匹配和 pass/fail 判断（v1.8 新增）
- **前5条错误**（`top_errors`）：从 `UVM_FATAL` / `UVM_ERROR` / 额外关键词错误中按出现顺序提取最多5条
- 每条错误记录包含：级别、时间戳、错误ID、文件位置、描述
- 描述提取：取本行描述，并最多向后追加3行续行（遇到 UVM 条目、空行或**非缩进行**停止）
- 多文件**并行解析**（`ThreadPoolExecutor`，`as_completed` 实时回调），批量场景性能优化
- 全量统计（FATAL/ERROR/WARNING 计数）基于全文所有错误行（即使 top_errors 已满仍继续扫描）
- **内存模式**：逐行流式读取（`pending` 状态机），内存占用与文件大小无关
- **`all_errors`**（v1.6 新增）：全文扫描所有 FATAL/ERROR/WARNING 的唯一 `(level, error_id)` 对，用于跨文件去重统计；仅存每种 ID 的首次出现记录，内存开销极低
- **`status`**（v1.6 新增，v1.8 重设计）：单文件 PASS/FAIL 状态；新逻辑见 3.12
- **`pass_found`**（v1.8 新增）：布尔值，文件中是否找到任意通过标记字符串（见 3.16）

### 3.3 知识库匹配

两阶段匹配，对每个日志的 **前5条 FATAL/ERROR 各自独立执行**，每条可命中多个知识库条目：

1. **精确匹配**：错误ID（忽略大小写）+ 错误类型完全一致；收集**所有**命中行
2. **关键词匹配**：`关键描述关键词`（中英文逗号分隔，AND 逻辑）全部出现在描述中；收集**所有**命中行

匹配结果：
- `status`：`matched` / `unmatched` / `no_error`
- `entries`：所有命中的知识库条目列表（同一错误可能对应多条根因），**按 `录入日期` 降序排列，最新条目排首位；缺失日期排末尾**（v1.4 新增）
- `entry`：`entries[0]`，即日期最新的命中条目，向后兼容

**汇总状态**（`r.match`）：有任意条 unmatched → `unmatched`；全部命中 → `matched`。

### 3.4 结果展示

- 顶部汇总栏：日志总数、**PASS/FAIL 数量**（v1.6，v1.8 重设计）、**去重后的** FATAL/ERROR/WARNING/额外关键词错误数（点击跳转去重详情页，见 3.13）、含未匹配错误的日志数
- **左侧文件导航**（v1.8 重设计）：**FAIL 日志在上**（始终展开），**PASS 日志在下**（默认折叠，以「▶ PASS (N)」分组头折叠；点击展开/收起）；默认高亮第一个 FAIL 文件（全 PASS 时高亮第一个 PASS）
- 左侧导航圆点颜色：🔴 有 FATAL；🟠 有 ERROR/额外关键词错误；🟡 仅 WARNING；🟢 PASS
- 右侧详情面板：错误统计（含额外关键词类型的统计卡）、**前5条 FATAL/ERROR/额外关键词错误列表**（每条独立展示匹配结果）
- 命中条目显示：默认展示 报错原因/根因分类/所属模块/录入人/解决方案/关联用例；点击「**显示全部 ▾**」展开 错误类型/错误ID/关键描述关键词/录入日期
- 多条命中：同一错误命中多个知识库条目时，显示「共 N 条」徽章，各条目以「— 根因 N —」分隔线展示，**按录入日期从新到旧排列**（v1.4 新增）

### 3.5 知识库回写

**三类回写场景均支持**（v1.5 扩展）：

**场景一：未匹配错误首次录入**
- 每条 unmatched 错误有独立回写表单，填写后新增一行到 Excel
- 写入后该错误在当前会话中标记为 `matched`（manual）

**场景二：已命中错误补充录入**（v1.3 新增）
- 命中知识库的错误匹配框右上角提供「+ 补充录入」按钮
- 展开独立表单，填写后在 Excel 新增独立一行（不覆盖已有条目）
- 成功后表单自动清空，可继续录入更多根因；刷新页面后新条目出现在「共 N 条」列表中

**场景三：直接添加条目**（v1.5 新增，见 3.9）

**回写字段**：

| 字段 | 必填 | 说明 |
|---|---|---|
| 错误类型 | **是**（添加/编辑） | UVM_FATAL / UVM_ERROR / UVM_WARNING / **额外关键词**（下拉候选项动态包含当前 `extra_patterns.json` 中的全部关键词）；回写表单中显示为下拉选择框（v1.6 新增）；首页「添加条目」Tab 中改为可输入+可选 combobox（v1.9 升级） |
| 关键描述关键词 | 否 | 逗号分隔，默认预填该条错误描述前50字符 |
| 报错原因 | **是** | 根因说明，不能为空，服务端校验 |
| 所属模块 | 否 | 默认预填错误位置文件名 |
| 根因分类 | **是** | 枚举：DUT Bug / TB Bug / 用例问题 / 工具问题 / 其他问题 |
| 解决方案 | 否 | 处理建议 |
| 关联用例 | 否 | 用例名称 |
| 录入人 | 否 | 默认预填操作系统当前用户名（`getpass.getuser()`，Windows/Linux 均支持），可手动修改 |

服务端输入校验：`错误类型` 必须为合法级别（UVM 三项 + 当前 `extra_patterns`），`报错原因` 不能为空，所有字段截断至 500 字符。写入时自动追加 `录入日期`。写操作通过双层锁保证并发安全。

**去重检测**（v1.5 新增，见 3.10）：所有写入路径（新增/编辑/回写）均在执行前检查重复，发现冲突时展示警告并提供「仍要写入」强制选项。

### 3.6 报告导出

- **Excel 报告**：含"汇总"Sheet + 各日志独立 Sheet（展示前5条错误及各自匹配结果，多条命中时逐条列出「— 根因 N —」），带颜色样式
- **HTML 报告**：自包含单文件，可直接发送给无工具的人员查阅；多条命中时逐条展示；所有动态内容经 HTML 转义，防止 XSS

### 3.7 知识库管理

- 默认知识库：`error_db.xlsx`（与 exe 同目录）
- 支持在 UI 填写自定义知识库路径（可指向网络共享盘）
- 知识库不存在时自动创建含样式表头的空白文件
- **编辑条目**（v1.5 新增）：结果页每条命中条目提供「✏ 编辑」按钮，展开内联编辑表单（预填所有字段），保存后直接更新 Excel 对应行
- **删除条目**（v1.5 新增）：结果页每条命中条目提供「🗑 删除」按钮，直接从 Excel 删除对应行并同步更新页面显示（无需刷新）；删除后页面底部显示 Toast 通知，8 秒内可点「撤销」恢复（v2.2 升级）
- **自动滚动备份**（v2.2 新增）：每次写入知识库前自动保留最多 3 份滚动备份（`.bak1` 最新、`.bak3` 最旧），备份文件与知识库同目录
- **备份恢复 UI**（v2.2 新增）：「解析配置」Tab 底部「📦 知识库备份」区域，显示现有备份列表（文件名 + 修改时间），可一键恢复至任意备份版本；恢复操作本身也会先备份当前状态，不会丢失数据

### 3.8 知识库查询（v1.3 新增）

首页第三个 Tab「🔎 查询知识库」，无需上传 log 即可直接搜索知识库：

| 查询条件 | 逻辑 | 说明 |
|---|---|---|
| 错误类型 | 精确过滤 | 不限 / UVM_FATAL / UVM_ERROR |
| 错误ID | 部分匹配（`in`） | 输入片段即可，如 `CNTR` |
| 描述/关键词 | 模糊：任意词命中 | 空格或逗号分隔；命中词越多排名越靠前 |

- 三个条件至少填一个
- 结果按命中词数量倒序排列，最多返回 100 条
- 结果内联展示在页面下方（无页面跳转），包含所有知识库字段
- 与分析模式共用知识库路径选择框，支持自定义知识库路径

### 3.9 直接添加知识库条目（v1.5 新增）

首页第四个 Tab「➕ 添加条目」，无需上传 log，直接向知识库追加新记录：

- 提供完整录入表单，字段与知识库 Schema 一致（见 4.1）
- `错误类型` 为**可输入可选**的 combobox（`<input list>`）：下拉候选项包含 UVM_ERROR / UVM_FATAL / UVM_WARNING 及当前 extra_patterns 中所有额外关键词，用户也可手动输入自定义值（v1.9 升级）
- `报错原因` 为必填项，服务端校验
- 写入成功后表单自动清空，可连续录入多条
- 「清空」按钮一键重置表单至默认状态
- 写入前执行去重检测（见 3.10），发现重复时展示警告

### 3.10 知识库去重检测（v1.5 新增）

所有写入路径（新增 `/kb/add`、编辑 `/kb/update`、回写 `/writeback`）均在执行前自动检查重复条目。

**重复判定规则**（满足其一即视为重复）：
- `错误类型` 相同 **AND** `错误ID` 相同（两者均非空，忽略大小写）
- `错误类型` 相同 **AND** `关键描述关键词` 相同（两者均非空，标准化逗号/空格后比较）
- `错误类型` 相同 **AND** `报错原因` 相同（两者均非空）（v1.6 新增）
- `错误类型` 相同 **AND** `解决方案` 相同（两者均非空）（v1.6 新增）

**不检查字段**：`录入人` 不参与去重比较。

**编辑时排除自身**：`/kb/update` 检测时自动排除当前被编辑行，避免与自身误报重复。

**前端交互**：
- 检测到重复时，在操作区显示黄色警告框，摘要展示第一条冲突条目（错误类型 / 错误ID / 报错原因前40字符）
- 提供「仍要写入」按钮：携带 `force: true` 强制写入，跳过去重检查
- 提供「取消」按钮：关闭警告，不执行写入

### 3.11 分析进度条与处理日志（v1.6 新增）

提交分析请求后，页面不再跳转，首页底部出现进度卡片，实时展示分析进度：

- **进度条**：水平进度条，0→50% 为解析阶段，50→100% 为匹配阶段；完成后变绿，出错后变红
- **进度文字**：每处理完一个文件后更新，显示"解析中：`<filename>`（done/total）"或"匹配中：`<filename>`"
- **处理日志区**：可滚动日志列表，每行显示一条处理事件（包含时间戳、文件名、FATAL/ERROR/WARNING 计数或命中/未命中数量）
- 进度数据通过 **SSE（Server-Sent Events）** 传输，无需 WebSocket，兼容内网部署
- **路径模式**新增 `scanning` 阶段（v1.8）：glob 展开在后台线程执行，`/analyze` 立即返回 `{job_id}`，前端显示"正在扫描文件..."

**技术实现**：
- `/analyze` 立即返回 `{job_id}`，实际解析在后台线程（`threading.Thread`）执行
- 前端用 `EventSource` 连接 `/progress/<job_id>`，每 0.3 秒推送一次 `_jobs[job_id]`
- 后台线程完成后将 `phase` 设置为 `'done'` 或 `'error'`；服务端在推送最终事件后 **`sleep(1)`** 再关闭连接，避免 Linux TCP FIN 提前到达
- 前端 `onerror` 回退到 **`/progress_status/<job_id>`** 单次 JSON 轮询（v1.8 新增），防止 SSE 连接中断时进度圈卡住

### 3.12 PASS/FAIL 判断逻辑（v1.6 新增，v1.8 重设计）

**配置了通过标记**（`pass_patterns.json` 非空，见 3.16）时：
- **PASS**：无任何非 WARNING 类型的错误（UVM_FATAL / UVM_ERROR / 额外关键词均为 0） **且** 文件中找到至少一条通过标记字符串
- **FAIL**：有任意错误 **或** 无错误但未找到通过标记

**未配置通过标记**（列表为空）时（退化为旧逻辑）：
- **PASS**：无任何非 WARNING 类型的错误
- **FAIL**：有任意错误

顶部汇总栏 PASS（绿）/ FAIL（红）卡片；导航圆点绿色（`dot-pass`）标示 PASS 文件。

### 3.13 错误去重统计与跳转（v1.6 新增）

顶部汇总栏的 FATAL/ERROR/WARNING 数量显示**去重后的唯一错误数**：

- 通过 `all_errors` 跨文件聚合，按 `(level, error_id)` 去重（相同 ID 在多个文件中只计1次）
- 数字可点击（链接样式），跳转到独立的去重详情页 `/errors?level=UVM_FATAL`（或 ERROR/WARNING）

**去重详情页（`/errors`）**：
- 展示该级别所有唯一错误，按出现文件数量降序排列
- 每条错误显示：序号、错误ID、描述、位置、出现文件数
- 文件标签（最多8个，超出显示"…还有 N 个"）为超链接，点击跳转到 `/result?focus=<filename>`（高亮该文件面板），支持右键在新标签页打开
- 提供「← 返回结果」按钮

### 3.14 文件链接右键打开（v1.6 新增）

去重详情页（`errors.html`）中的文件标签渲染为 `<a>` 超链接而非纯文本标签，支持浏览器原生右键菜单（"在新标签页中打开"等）。

### 3.15 额外错误关键词管理（v1.8 新增）

首页「⚙ 解析配置」Tab，支持管理任意「行首关键词 + 冒号」格式的额外错误类型：

- **匹配格式**：`^关键词: 描述内容`（行首 + 冒号，不区分大小写，UVM 正则优先，generic 正则兜底）
- **与 UVM 等价**：计入错误统计、进入 top_errors、参与知识库匹配、影响 pass/fail 判断
- **默认关键词**：`ERROR`、`FATAL`、`FAILED`、`VIRL_MEM_WARNING`、`JVP TEST FAILED`（首次运行时无配置文件时使用）
- **配置文件**：`extra_patterns.json`（与 exe 同目录），自动创建，JSON 字符串数组
- **UI 管理**：支持增删改，即时生效（下次分析时使用新列表）；关键词仅允许大写字母、数字和下划线
- **动态化影响**：结果页统计卡、错误类型下拉框、`_valid_levels()` 校验集合均动态包含当前关键词列表

### 3.16 通过标记配置（v1.8 新增）

「⚙ 解析配置」Tab 第二节，支持管理任意字符串作为 PASS 判定标记：

- **检测方式**：全文扫描（内联于解析循环），任意行包含任意一条标记字符串即视为 `pass_found=True`
- **默认标记**：`JVP TEST PASSED`（首次运行时无配置文件时使用）
- **配置文件**：`pass_patterns.json`（与 exe 同目录），自动创建，JSON 字符串数组
- **UI 管理**：支持增删改，即时生效；标记字符串无格式限制，可包含空格和特殊字符
- **空列表行为**：退化为旧逻辑（只看有无错误，忽略 pass_found）

### 3.17 知识库备份与撤销删除（v2.2 新增）

**撤销删除**：

- 删除条目操作无需预先 `confirm()`，执行后页面底部显示 Toast「已删除 `<类型> / <ID>`」
- Toast 8 秒内提供「撤销」按钮，点击后恢复该条目并刷新页面
- 撤销缓冲存于进程内存（`_undo_buffers[sid]`），每会话最多保留 10 条，程序重启后清空
- 路由：`POST /kb/undo_delete`

**写入前自动滚动备份**：

每次调用任何写入操作（追加、编辑、删除）前，自动将知识库文件轮转备份：

```
当前文件 → bak1,  bak1 → bak2,  bak2 → bak3  （旧 bak3 丢弃）
```

- 最多保留 3 份（`.bak1` 最新、`.bak3` 最旧），`.bak1` 始终是上一次写入前的状态
- 备份通过 `shutil.copy2()` 实现；磁盘满等 `OSError` 静默跳过，不影响主写入
- 恢复操作本身也触发一次备份轮转（自动将当前状态存入 `.bak1`），恢复后数据可再次撤销

**备份恢复 UI**：

- 「解析配置」Tab 切换时自动查询 `GET /kb/backups`
- 列表展示每份备份的文件名与最后修改时间；无备份时显示提示
- 每份备份有「恢复」按钮，confirm 确认后调用 `POST /kb/restore_backup`，成功后刷新列表

---

## 4. 数据结构

### 4.1 知识库 Schema（`error_db.xlsx`）

| 列名 | 类型 | 说明 |
|---|---|---|
| 错误类型 | 字符串 | `UVM_FATAL` / `UVM_ERROR` / `UVM_WARNING` |
| 错误ID | 字符串 | UVM 日志中方括号内的 ID，用于精确匹配 |
| 关键描述关键词 | 字符串 | 中英文逗号分隔，ALL 关键词均需命中（AND 逻辑） |
| 报错原因 | 字符串 | 根因说明 |
| 所属模块 | 字符串 | 出错模块名 |
| 根因分类 | 字符串 | DUT Bug / TB Bug / 用例问题 / 工具问题 / 其他问题 |
| 解决方案 | 字符串 | 处理建议 |
| 关联用例 | 字符串 | 相关测试用例名 |
| 录入人 | 字符串 | 姓名 |
| 录入日期 | 字符串 | `YYYY-MM-DD`，写入时自动生成 |

同一错误可在知识库中存在**多行**（不同根因/解决方案），每行均为独立记录，匹配时全部返回。

### 4.2 内存数据结构（单次分析会话，v1.6）

```python
{
  'file':       str,          # 显示文件名（basename）
  'filepath':   str,          # 服务器上的完整路径
  'statistics': {'UVM_FATAL': int, 'UVM_ERROR': int, 'UVM_WARNING': int,
                 # v1.8：动态包含 extra_patterns 中的关键词
                 '<KEYWORD>': int, ...},
  'status':     str,          # 'pass' 或 'fail'（v1.8 重设计，见 3.12）
  'pass_found': bool,         # 是否找到通过标记字符串（v1.8 新增）
  'top_errors': [             # 按出现顺序的前5条 FATAL/ERROR（WARNING 不在此列表）
    {
      'level':       str,     # UVM_FATAL / UVM_ERROR / 额外关键词（v1.8）
      'timestamp':   str,     # 额外关键词错误时为空字符串
      'error_id':    str,
      'location':    str,
      'description': str,
      'match': {
        'status':   str,      # matched / unmatched / no_error
        'match_by': str,      # error_id / keywords / manual（仅 matched）
        'entry':    dict | None,   # entries[0]，向后兼容
        'entries':  list[dict],    # 所有命中知识库条目（可能多条）
      }
    },
    ...                       # 最多5条
  ],
  'all_errors': [             # 全文去重唯一错误（含 WARNING）（v1.6 新增）
    {
      'level':       str,     # UVM_FATAL / UVM_ERROR / UVM_WARNING
      'error_id':    str,
      'description': str,
      'location':    str,
    },
    ...
  ],
  'match': {                  # 汇总状态：有任意 unmatched → unmatched；全部命中 → matched
    'status':   str,
    'match_by': str,
    'entry':    dict | None,
    'entries':  list[dict],
  }
}
```

会话数据存于模块级 `_store` dict，TTL 为 2 小时，过期后自动清理。

后台任务状态存于模块级 `_jobs` dict，TTL 为 1 小时（`_JOBS_TTL = 3600`）：

```python
_jobs[job_id] = {
  'phase': str,   # 'scanning'（v1.8 路径模式 glob 展开中）| 'pending' | 'parsing' | 'matching' | 'done' | 'error'
  'pct':   int,   # 0~100
  'total': int,   # 文件总数（路径模式扫描完成前为 0）
  'logs':  list,  # 处理日志条目列表
  'error': str,   # 仅 phase=='error' 时
}
```

---

## 5. 技术约束

| 约束项 | 说明 |
|---|---|
| 运行环境 | Windows（exe）或 Linux（python 直接运行），内网无 PyPI 访问 |
| 依赖 | `flask`、`openpyxl`（均通过离线 wheel 安装或打包进 exe） |
| 打包 | PyInstaller `--onefile`；`sys.frozen` 判断区分运行模式（仅 Windows） |
| 并发写安全 | `threading.Lock`（进程内）+ `_FileLock`（跨进程，基于 `.lock` 文件 + `O_EXCL`） |
| 会话隔离 | 模块级 dict `_store`，key 为 Flask session UUID，TTL 2 小时 |
| 上传文件清理 | 上传模式：解析完成后**立即删除**临时文件（结果已存入内存，文件不再需要）；启动时额外清理 `uploads/` 和 `reports/` 下超过24小时的文件作为兜底 |
| 安全 | `secure_filename` 防路径穿越；随机持久化 `secret_key`；HTML 报告全字段 `html.escape`；writeback 服务端输入校验；`根因分类` 枚举归一化防数据污染 |
| 标准库优先 | 并发锁、报告 HTML、glob 展开均使用标准库；新功能开发优先评估标准库可行性 |
| SSE / 后台线程 | 进度推送使用 Flask SSE（`text/event-stream`），无需额外依赖；后台解析使用 `threading.Thread`，`_jobs` dict 由 `_jobs_lock` 保护（清理和创建），`_store` 由 `_store_lock` 保护；`sid`（session ID）在后台线程启动前提取，避免跨线程访问 Flask session；SSE 前端有 30 秒无活动超时兜底 |

---

## 6. 接口清单（Flask 路由）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页（上传/路径/查询/添加/解析配置五模式界面） |
| POST | `/analyze` | 接收日志（上传或路径），启动后台线程，立即返回 `{job_id: str}`。路径模式 glob 展开在后台进行（v1.8）。表单字段：`db_path`、`logs[]`（上传模式）或 `path_mode=1` + `log_paths`（路径模式） |
| GET | `/progress/<job_id>` | SSE 进度流。每 0.3 秒推送一条 `data: {phase, pct, logs, error?}\n\n`；`phase=='done'` 或 `'error'` 后 sleep(1) 再关闭流（v1.6 新增，v1.8 加 sleep 防 Linux 竞争） |
| GET | `/progress_status/<job_id>` | 单次 JSON 轮询任务最终状态，供 SSE onerror 兜底使用（v1.8 新增） |
| GET | `/result` | 分析结果页 |
| GET | `/errors` | 去重错误详情页。查询参数 `level=<任意 level 字符串>`，展示该级别所有唯一错误及所在文件列表（v1.6 新增，v1.8 扩展支持额外关键词 level） |
| POST | `/writeback` | 写回一条知识库记录（含未匹配首次录入和已匹配补充录入）。JSON 字段含 `file_name`、`error_idx`、`level`、`reason`（必填）、`force`（可选，跳过去重）等。返回 `{success, duplicate?, conflicts?, error?}` |
| POST | `/query` | 知识库模糊查询。JSON 字段：`db_path`（可选）、`level`（可选）、`error_id`（可选，部分匹配）、`text`（可选，任意词模糊）。返回 `{entries: list, total: int}` |
| POST | `/kb/add` | 直接追加知识库条目（不依赖会话）。JSON 字段：`db_path`（可选）、`错误类型`（必填）、`报错原因`（必填）及其余知识库字段、`force`（可选）。返回 `{success, duplicate?, conflicts?, error?}` |
| POST | `/kb/update` | 编辑知识库指定行。JSON 字段：`row_idx`（Excel 行号）、`db_path`（可选）、需更新的字段、`force`（可选）。返回 `{success, duplicate?, conflicts?, error?}` |
| POST | `/kb/delete` | 删除知识库指定行。JSON 字段：`row_idx`、`db_path`（可选）。返回 `{success, entry_label, error?}`；删除前自动入撤销栈（v2.2） |
| POST | `/kb/undo_delete` | 撤销最近一次删除，从当前会话内存缓冲中恢复被删条目。返回 `{success, error?}`（v2.2 新增） |
| GET | `/kb/backups` | 查询当前知识库路径下可用的滚动备份列表。返回 `{backups: [{idx, filename, mtime, mtime_str}]}`（v2.2 新增） |
| POST | `/kb/restore_backup` | 从指定备份（`backup_idx` 1–3）恢复知识库；恢复前自动备份当前状态。返回 `{success, error?}`（v2.2 新增） |
| GET | `/export/excel` | 下载 Excel 报告 |
| GET | `/export/html` | 下载 HTML 报告 |
| GET | `/extra_patterns` | 返回当前额外错误关键词列表 `{patterns: list}` （v1.8 新增） |
| POST | `/extra_patterns/add` | 添加关键词 `{keyword: str}`（v1.8 新增） |
| POST | `/extra_patterns/delete` | 删除关键词 `{keyword: str}`（v1.8 新增） |
| POST | `/extra_patterns/update` | 重命名关键词 `{old: str, new: str}`（v1.8 新增） |
| GET | `/pass_patterns` | 返回当前通过标记列表 `{patterns: list}`（v1.8 新增） |
| POST | `/pass_patterns/add` | 添加通过标记 `{pattern: str}`（v1.8 新增） |
| POST | `/pass_patterns/delete` | 删除通过标记 `{pattern: str}`（v1.8 新增） |
| POST | `/pass_patterns/update` | 修改通过标记 `{old: str, new: str}`（v1.8 新增） |

---

## 7. 已知限制与待决策项

| 编号 | 描述 | 当前状态 |
|---|---|---|
| L-01 | 会话数据存内存，重启 exe 后分析结果丢失，无法回溯历史 | 已知，设计如此 |
| L-02 | ~~仅取"首错"匹配，多条错误的全量匹配暂不支持~~ | **v1.2 已解决**：改为提取前5条错误逐一匹配 |
| L-03 | ~~知识库只能追加，不支持在 UI 中编辑或删除已有条目~~ | **v1.5 已解决**：结果页支持编辑（`/kb/update`）和删除（`/kb/delete`）；首页新增直接添加 Tab（`/kb/add`） |
| L-04 | ~~上传文件永久保存在 `uploads/` 目录，无自动清理机制~~ | **v1.2 已解决**：解析完成后立即删除；启动时清理24小时以上旧文件作为兜底 |
| L-05 | `_FileLock` 超时（默认15秒）时写操作失败，前端提示错误但不重试 | 待评估 |
| L-06 | ~~根因分类为硬编码枚举，新增类别需改代码~~ | **v1.3 已更新**：枚举调整为 DUT Bug / TB Bug / 用例问题 / 工具问题 / 其他问题 |
| L-07 | 路径模式下，同名文件（来自不同目录）在结果页仅以 basename 区分，可能产生混淆 | 已知，轻微 |
| L-08 | ~~上传文件清理仅在启动时执行一次，长期不重启时文件持续累积~~ | **v1.2 已解决**：改为解析完成后立即删除，不再依赖重启触发清理 |
| L-09 | 知识库查询结果只读展示，查询页面不支持直接从查询结果补充录入 | 待评估 |

---

## 8. 变更记录

| 版本 | 日期 | 变更内容 | 涉及文件 |
|---|---|---|---|
| v1.0 | 2026-03-08 | 初始版本，建立 PRD 基准文档 | PRD.md |
| v1.1 | 2026-03-08 | **并发安全**：新增双层锁（进程内线程锁 + 跨进程文件锁），`load_db` 读取重试 | `core/db_manager.py` |
| v1.1 | 2026-03-08 | **PyInstaller 打包支持**：`sys.frozen` 路径适配，启动自动打开浏览器 | `app.py`, `triage_tool.spec` |
| v1.2 | 2026-03-12 | **前5条错误匹配**：`top_errors` 替代 `first_error`，每条独立匹配知识库；UI 展示多条错误及各自匹配结果和回写表单 | `core/log_parser.py`, `core/matcher.py`, `app.py`, `templates/result.html`, `core/reporter.py` |
| v1.2 | 2026-03-12 | **指定路径分析**：新增服务器本地路径输入模式，支持 glob 通配符，最多100文件，零拷贝直读 | `app.py`, `templates/index.html`, `static/style.css` |
| v1.2 | 2026-03-12 | **并行解析**：`ThreadPoolExecutor` 并行解析多日志文件 | `core/log_parser.py` |
| v1.2 | 2026-03-12 | **多行续行描述**：描述提取最多合并3行续行 | `core/log_parser.py` |
| v1.2 | 2026-03-12 | **关键词中文逗号**：`关键描述关键词` 同时支持中英文逗号分隔 | `core/matcher.py` |
| v1.2 | 2026-03-12 | **安全加固**：`secure_filename` 防路径穿越；随机持久化 `secret_key`（Linux 下 chmod 0o600）；HTML 报告 `html.escape` 防 XSS；writeback 服务端输入校验 | `app.py`, `core/reporter.py` |
| v1.2 | 2026-03-12 | **文件大小限制**：上传模式单文件限制 10 GB | `app.py` |
| v1.2 | 2026-03-12 | **临时文件清理**：启动时清理 uploads/ 和 reports/ 下超过24小时的文件 | `app.py` |
| v1.2 | 2026-03-12 | **会话 TTL**：会话数据2小时自动过期清理 | `app.py` |
| v1.2 | 2026-03-12 | **跨平台适配**：`send_file` 改用 `download_name`；stale lock 删除捕获 `OSError`（Windows 兼容） | `app.py`, `core/db_manager.py` |
| v1.2 | 2026-03-12 | **上传文件即时清理**：上传模式解析完成后立即删除 `uploads/` 临时文件，不再依赖重启触发；启动时清理保留作为兜底 | `app.py` |
| v1.3 | 2026-03-16 | **流式解析**：`log_parser` 改为逐行流式读取（`pending` 状态机），内存占用与文件大小无关，支持 10GB+ 日志 | `core/log_parser.py` |
| v1.3 | 2026-03-16 | **WARNING 不参与匹配**：`UVM_WARNING` 仅统计计数，不进入 `top_errors`，不做知识库匹配 | `core/log_parser.py` |
| v1.3 | 2026-03-16 | **多条命中展示**：matcher 收集所有命中知识库行（`entries` 列表），UI 逐条展示，标题显示「共 N 条」 | `core/matcher.py`, `templates/result.html`, `static/style.css` |
| v1.3 | 2026-03-16 | **命中条目展开**：命中匹配框增加「显示全部 ▾」按钮，展开 错误类型/错误ID/关键描述关键词/录入日期 | `templates/result.html`, `static/style.css` |
| v1.3 | 2026-03-16 | **命中条目补充录入**：命中错误支持「+ 补充录入」，向知识库追加新根因行，不覆盖已有条目 | `app.py`, `templates/result.html`, `static/style.css` |
| v1.3 | 2026-03-16 | **根因分类更新**：`环境问题` 替换为 `工具问题` / `其他问题` | `templates/result.html` |
| v1.3 | 2026-03-16 | **路径模式文件上限**：从100提升至5000 | `app.py`, `templates/index.html` |
| v1.3 | 2026-03-16 | **知识库查询**：新增第三 Tab，支持按错误类型/错误ID/描述关键词模糊查询知识库，结果按相关度排序，内联展示 | `app.py`, `templates/index.html`, `static/style.css` |
| v1.3 | 2026-03-16 | **报告多条命中**：Excel/HTML 报告同步展示所有命中知识库条目（与 Web UI 一致），修复仅显示首条的缺陷 | `core/reporter.py` |
| v1.4 | 2026-03-17 | **命中条目按录入日期排序**：两阶段匹配（精确/关键词）命中的所有条目按 `录入日期` 降序排列，最新条目显示在最前；缺失日期排末尾；`entry`（首条）始终为日期最新的命中记录 | `core/matcher.py` |
| v1.4 | 2026-03-17 | **端口占用友好提示**：启动时端口被占用（errno 98/10048）捕获 OSError，在 terminal 打印解决步骤（换端口/查找并终止占用进程），覆盖 Linux 和 Windows | `app.py` |
| v1.5 | 2026-03-17 | **编辑知识库条目**：`load_db` 返回 `_row_idx`；结果页命中条目提供「✏ 编辑」内联表单，预填全字段，保存写入 `/kb/update` 直接更新 Excel 对应行 | `core/db_manager.py`, `app.py`, `templates/result.html`, `static/style.css` |
| v1.5 | 2026-03-17 | **删除知识库条目**：结果页命中条目提供「🗑 删除」按钮，确认后调用 `/kb/delete` 删除 Excel 行，同步更新页面（移除条目卡片、更新「共 N 条」计数） | `core/db_manager.py`, `app.py`, `templates/result.html` |
| v1.5 | 2026-03-17 | **直接添加条目**：首页新增第四 Tab「➕ 添加条目」，提供完整录入表单，提交至 `/kb/add` 直接写入知识库（不依赖会话），成功后自动清空表单 | `app.py`, `templates/index.html` |
| v1.5 | 2026-03-17 | **写入去重检测**：`find_duplicates` 在 `/kb/add`、`/kb/update`、`/writeback` 写入前检查重复（同错误类型+错误ID 或 同错误类型+关键描述关键词），发现冲突返回 `duplicate:true` 及冲突摘要；前端展示黄色警告框，提供「仍要写入」（`force:true`）和「取消」 | `core/db_manager.py`, `app.py`, `templates/index.html`, `templates/result.html`, `static/style.css` |
| v1.6 | 2026-03-23 | **去重规则扩展**：`find_duplicates` 新增 `报错原因` 和 `解决方案` 作为去重字段（均非空时相同即视为重复）；`录入人` 明确不参与去重比较 | `core/db_manager.py` |
| v1.6 | 2026-03-23 | **回写表单错误类型下拉**：未匹配首次录入和已命中补充录入两类回写表单中，`错误类型` 改为 `<select>` 下拉（预选解析到的级别），用户可手动修正 | `templates/result.html` |
| v1.6 | 2026-03-23 | **实时分析进度条**：`/analyze` 改为异步，立即返回 `{job_id}`；后台线程执行解析+匹配，通过 `/progress/<job_id>` SSE 推送阶段/百分比/日志；首页进度卡片实时展示，完成后自动跳转 `/result` | `app.py`, `core/log_parser.py`, `core/matcher.py`, `templates/index.html`, `static/style.css` |
| v1.6 | 2026-03-23 | **PASS/FAIL 统计**：`parse_log` 新增 `status` 字段；结果页汇总栏新增 PASS（绿）/FAIL（红）卡片；左侧导航圆点新增绿色 `dot-pass` | `core/log_parser.py`, `app.py`, `templates/result.html`, `static/style.css` |
| v1.6 | 2026-03-23 | **去重错误统计与跳转**：`parse_log` 新增 `all_errors` 字段；`/result` 路由计算跨文件去重唯一计数；汇总栏 FATAL/ERROR/WARNING 显示去重数并可点击跳转 `/errors` 详情页；详情页按出现文件数降序列出唯一错误 | `core/log_parser.py`, `app.py`, `templates/result.html`, `templates/errors.html`, `static/style.css` |
| v1.6 | 2026-03-23 | **文件标签超链接**：`errors.html` 文件标签改为 `<a href="/result?focus=...">` 超链接，支持右键在新标签页打开；Jinja2 注册 `urlencode` 自定义过滤器用于 URL 安全编码文件名 | `templates/errors.html`, `app.py` |
| v1.7 | 2026-03-26 | **录入人自动预填**：启动时通过 `getpass.getuser()` 获取操作系统当前用户名（Windows 读 `USERNAME` 环境变量，Linux 读 `USER`/`LOGNAME` 或 `pwd` 模块），注入全局变量 `OS_USERNAME`；未匹配回写表单、已命中补充录入表单、首页「添加条目」Tab 三处「录入人」输入框自动预填，用户可手动修改；获取失败时降级为空字符串不影响功能 | `app.py`, `templates/result.html`, `templates/index.html` |
| v1.8 | 2026-03-29 | **额外错误关键词支持**：`log_parser` 新增 `_build_gen_pattern()` 和 `extra_keywords` 参数，支持任意行首关键词+冒号格式的错误行（UVM 优先，generic 兜底）；`statistics` 动态扩展；错误条目进入 top_errors 参与 KB 匹配和 pass/fail；`extra_patterns.json` 配置文件 + 首页「⚙ 解析配置」Tab 增删改 UI | `core/log_parser.py`, `app.py`, `templates/index.html`, `templates/result.html`, `static/style.css` |
| v1.8 | 2026-03-29 | **通过标记配置（pass_patterns）**：`log_parser` 新增 `pass_patterns` 参数和 `pass_found` 返回字段，全文扫描内联检测；`pass_patterns.json` 配置文件（默认 `JVP TEST PASSED`）+ UI 增删改；空列表时退化为旧逻辑 | `core/log_parser.py`, `app.py`, `templates/index.html` |
| v1.8 | 2026-03-29 | **PASS/FAIL 逻辑重设计**：PASS = 无任何非 WARNING 错误 AND 找到通过标记；FAIL = 有错误 OR 无错误但无通过标记；无通过标记配置时退化（只看有无错误） | `core/log_parser.py` |
| v1.8 | 2026-03-29 | **结果页 FAIL/PASS 分组导航**：左侧导航 FAIL 日志在上（始终展开），PASS 日志折叠于「▶ PASS (N)」分组头下（默认收起）；默认激活第一个 FAIL（全 PASS 时激活第一个 PASS）；`focus=` URL 跳转修复（改用 `data-idx` 属性而非 DOM 顺序索引，避免重排后错位；自动展开 pass 组） | `templates/result.html`, `static/style.css` |
| v1.8 | 2026-03-29 | **动态统计与下拉框**：结果页统计卡、错误类型下拉框、去重跳转链接均动态包含 extra_patterns；badge/stat-chip 新增兜底灰色和紫色 extra 样式；`_unique_error_counts()` 动态化；`_valid_levels()` 动态化 | `app.py`, `templates/result.html`, `static/style.css` |
| v1.9 | 2026-03-30 | **默认额外关键词扩充**：`_EXTRA_PATTERNS_DEFAULT` 从 `['ERROR','FATAL','FAILED']` 扩展为 `['ERROR','FATAL','FAILED','VIRL_MEM_WARNING','JVP TEST FAILED']`，覆盖常见 JVP 测试失败和内存警告格式 | `app.py` |
| v1.9 | 2026-03-30 | **查询/添加条目错误类型动态化**：首页「查询知识库」`qLevel` 下拉和「添加条目」`addType` 均在页面加载时自动追加当前 extra_patterns；配置变更后实时同步（通过 `kwRender` 回调）；`_populateLevelSelects()` 函数统一管理 | `templates/index.html` |
| v1.9 | 2026-03-30 | **添加条目错误类型改为 combobox**：`addType` 由 `<select>` 改为 `<input list>` + `<datalist>`，用户既可下拉选择预设值，也可手动输入任意自定义错误类型 | `templates/index.html` |
| v2.0 | 2026-04-02 | **代码架构重构（Flask Blueprint 拆分）**：`app.py` 从 ~964 行缩减为 ~100 行；所有路由拆分为 5 个 Blueprint：`blueprints/analysis.py`（7条路由含后台线程 `_run_analysis`）、`blueprints/writeback.py`（1条）、`blueprints/kb.py`（4条）、`blueprints/config_bp.py`（8条）、`blueprints/export.py`（2条）；新增 `state.py` 集中管理所有共享状态（`_store`/`_jobs`/`EXTRA_PATTERNS`/`PASS_PATTERNS` 及所有工具函数）；无任何功能变更 | `app.py`, `state.py`, `blueprints/` |
| v2.1 | 2026-04-02 | **根因分类枚举校验（L-I-3）**：`/writeback` 端点对 `category` 字段做大小写不敏感归一化，匹配 `DUT Bug/TB Bug/用例问题/工具问题/其他问题` 枚举，未知值自动兜底为「其他问题」，防止 LLM 或自由输入污染知识库 | `blueprints/writeback.py` |
| v2.1 | 2026-04-02 | **LLM 会话字段预留（L-I-1）**：`_store[sid]` 新增 `file_paths`（路径模式文件列表）、`p3_history`（多轮对话历史）、`p3_tokens`（token 计数）字段；`_set_results()` 改为合并写入（不覆盖已有 LLM 字段）；`_run_analysis` 在路径模式下传入 `file_paths` | `state.py`, `blueprints/analysis.py` |
| v2.1 | 2026-04-02 | **常量集中管理（M-3）**：`TOP_N = 5` 和 `MAX_LEN = 500` 统一在 `state.py` 定义；`blueprints/writeback.py` 和 `blueprints/kb.py` 改为引用 `state.MAX_LEN`，消除重复定义 | `state.py`, `blueprints/writeback.py`, `blueprints/kb.py` |
| v2.1 | 2026-04-02 | **自动化测试基础设施（L-3）**：新增 `tests/` 目录，包含 `conftest.py`（fixtures）、`test_log_parser.py`（14 tests）、`test_matcher.py`（11 tests）、`test_db_manager.py`（14 tests）、`test_api.py`（12 tests）和 `pytest.ini`；pytest 需从互联网机器 `pip download pytest -d ./packages` 后安装 | `tests/` |
| v2.2 | 2026-04-02 | **知识库滚动备份**：`_save_atomic()` 写入前自动轮转最多 3 份滚动备份（`.bak1` 最新，`.bak3` 最旧）；新增 `_rotate_backups()` 和 `restore_backup()` 函数；备份失败（磁盘满等）静默跳过，不影响主写入 | `core/db_manager.py` |
| v2.2 | 2026-04-02 | **撤销删除（Toast + 内存缓冲）**：删除知识库条目不再弹 `confirm()`，改为底部 Toast「已删除 X / Y」+ 8 秒内可点「撤销」恢复；后端 `_undo_buffers[sid]` 内存栈（每会话最多 10 条，重启后清空）；新增路由 `POST /kb/undo_delete` | `state.py`, `blueprints/kb.py`, `templates/result.html`, `static/style.css` |
| v2.2 | 2026-04-02 | **解析配置 Tab 备份恢复面板**：「解析配置」Tab 底部新增「📦 知识库备份」区域，显示最多 3 份备份的文件名和修改时间；每份备份有「恢复」按钮，确认后调用 `POST /kb/restore_backup`；恢复前自动备份当前状态，不丢数据；新增路由 `GET /kb/backups`、`POST /kb/restore_backup` | `blueprints/kb.py`, `templates/index.html` |
| v2.3 | 2026-04-04 | **LLM 增强层落地**：在规则引擎之上新增可选 LLM 层。覆盖 P0 配置 GUI / P1 多条匹配重排 / P2 AI 日志问答 / P3 相似错误推荐 / P4 批量模式分析 / P5 语义知识库搜索 / P6 知识库质检+AI 合并。基础/增强模式按钮一键切换；`llm_config.json` 配置文件 + 14 路由 + 多个 AI 按钮（在「未匹配」「多条命中」错误块、结果页顶栏、AI 功能 Tab）。详细技术架构见 `LLM_INTEGRATION_PLAN.md`，用户指南见 `LLM_USAGE_GUIDE.md` | `core/llm_client.py`, `blueprints/llm_bp.py`, `templates/result.html`, `templates/index.html`, `static/style.css` |
| v2.3 | 2026-04-04 | **P1 Testlist 导出**：「智能推荐」结果点击「📋 导出 Testlist」生成回归测试列表，4 列参数（`RUN_NUM`/`WAVE`/`COV`/`SVSEED`）支持批量配置 + 单条覆盖，浏览器内预览、复制文本、下载 `.txt`（Chrome/Edge 可选保存路径，Firefox 默认目录） | `templates/result.html`（纯前端实现） |
| v2.3 | 2026-04-05 | **内网 LLM 移植**：HTTP 层从 `requests` 改为 stdlib `urllib`，`urllib.request.ProxyHandler({})` 强制绕开系统代理（避开 `http_proxy` 干扰内网调用）；Anthropic 端点头自动补 `x-api-key` + `anthropic-version: 2023-06-01`；OpenAI `/v1` 自动补 `/chat/completions`；零第三方依赖适配纯离线内网 | `core/llm_client.py` |
| v2.3 | 2026-04-06 | **原生 KB 文件选择器**：知识库路径输入栏的「选择文件」按钮改为弹原生 OS 文件对话框（`tkinter` 子进程，30s 超时兜底；无 tkinter / 无 DISPLAY 时降级提示手输）；`install_packages.py` 同时支持 pip wheels 和系统包（`python3-tk` 等 `*.deb` / `*.rpm`，离线内网部署一键装）；新增 `DEPLOYMENT.md` 详述内网完整部署流程 | `blueprints/kb.py`, `core/file_picker.py`, `templates/index.html`, `install_packages.py`, `DEPLOYMENT.md` |
| v2.3 | 2026-05-08 | **移除"未匹配错误自动分析"功能**：原 P1 功能（AI 自动预填回写表单 5 字段）下线——日志原文已在用户眼前、AI 推断价值低且需人工核对，反而增加流程。后续 P 编号顺延：原 P2~P7 → P1~P6；删除路由 `/llm/analyze_error` 后总路由仍为 14 条 | `blueprints/llm_bp.py`, `templates/result.html`, `static/style.css`, `LLM_INTEGRATION_PLAN.md`, `LLM_USAGE_GUIDE.md` |
| v2.3 | 2026-05-09 | **P2 AI 日志问答 — 锚点定位准确性 6 项改进**：(A) 中文 query 关键词提取 + 中→英同义词扩展（"报错"→`error/fatal`、"超时"→`timeout` 等 30+ 映射）；(B) `_PREFER_END_PAT` 对偶 START，识别"最后/结束前/last/final"等意图；(C) 锚点权重表替代 1-vote 计数（FATAL=5/ERROR=3/WARNING=1/extra=+2/kw=+6/path=+2）；(D) 多块聚簇 + 预算分配——锚点散布远端时返回多段而非一整段（典型 "dut_cfg" 类查询噪声从 2491 行 → ~200 行）；(E) 文件路径命中（如 `axi_driver.sv`）额外加权；(F) `coverage_warning` 改用窗外估算去除误报；新增 61 项单元测试 | `blueprints/llm_bp.py`, `templates/result.html`, `static/style.css`, `tests/test_llm_prescan.py` |
| v2.3 | 2026-05-09 | **P2 自适应 `p3_max_lines`**：从 `context_window × p3_chars_per_token × 0.7 / 平均行字符数` 反推安全行数上限并与用户配置 `p3_max_lines` 取小——既自动放大（接 Claude 200K 时窗口扩到 5500+ 行），也安全钳制（用户配 50000 + 模型 8K 时钳到 ~870 行）；最低 100 行兜底，避免极小 context 下取段为 0 | `blueprints/llm_bp.py` |
| v2.3 | 2026-05-09 | **P2 扩展到上传模式 + 多文件选择**：上传文件不再"解析完即删"，会话级保留（依赖现有 24h 孤儿扫除 + 2h `_STORE_TTL`）；每个文件结果块右侧独立的「🤖 AI 问答」按钮（多文件场景下用户可分别查询各文件）；切换文件时对话历史自动重置（前端发 `clear=true`）；`/llm/custom_extract` 新增 `file_index` 参数 + 越界校验；路径模式 glob 多文件场景同步获益（之前静默用 `file_paths[0]`） | `blueprints/analysis.py`, `blueprints/llm_bp.py`, `templates/result.html`, `static/style.css` |
| v2.3 | 2026-05-09 | **多 LLM profile 管理**：`llm_config.json` schema 升级为 `{active_profile, profiles: [...]}`，可保存 GLM-4.7 / Qwen-Max / Claude 等多套配置，UI 顶部下拉切换激活；老扁平格式首次启动自动迁移并写回；`p3_max_lines`、`endpoint`、`api_key` 等字段每 profile 独立。新增 4 路由：`POST /llm/profile/{add,update,delete,activate}`；最后一个 profile 拒绝删除；保存 / 切换 / 删除激活 profile 时若有 AI 后台任务（如 P6 知识库质检）运行会被禁止 + 弹窗确认强制（`force:true`）。LLM 路由总数 14 → 18；增 18 项单元测试 | `core/llm_client.py`, `blueprints/llm_bp.py`, `templates/index.html`, `tests/test_llm_profiles.py` |
| v2.3 | 2026-05-10 | **KB 稳定 ID + 活跃度加权排序**：`error_db.xlsx` 新增 `稳定ID` 列（行字段 hash 12 字符），`core/kb_migrate.py` 首次启动幂等回填存量条目；每次匹配 / 写回往 `kb_hits.jsonl` append 事件；`core/kb_stats.py` 聚合为时间衰减"活跃度 boost"，`matcher.run_match` 在 (error_id+level) 或 (关键词全 match) 多条命中时按 (boost desc, 日期 desc) 排序——团队真在用的条目优先出现；`score_query`（P3/P5 用）`final_score = relevance × (1 + 0.5 × activity_boost)`；事件文件 mtime 缓存、复用 `_FileLock` 跨进程安全、180 天前事件可归档到 `kb_hits_archive.jsonl`；增 14 项单元测试 | `core/kb_stats.py`, `core/kb_migrate.py`, `core/db_manager.py`, `core/matcher.py`, `tests/test_kb_stats.py` |
