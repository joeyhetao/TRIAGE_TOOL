# 仿真日志分类分诊工具 — 产品需求文档（PRD）

**文档版本**：v1.7
**基准代码版本**：2026-03-26
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

## 3. 当前功能（v1.6）

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

### 3.2 日志解析

**UVM 日志格式**：
```
UVM_ERROR /path/file.sv(142) @ 1000ns: uvm_test_top.env [ID] message
```

- 解析每个日志文件，提取所有 `UVM_FATAL` / `UVM_ERROR` / `UVM_WARNING` 条目
- **`UVM_WARNING` 仅统计计数，不进入 `top_errors` 列表，不参与知识库匹配**（v1.3 变更）
- **前5条错误**（`top_errors`）：从 `UVM_FATAL` / `UVM_ERROR` 中按出现顺序提取最多5条
- 每条错误记录包含：级别、时间戳、错误ID、文件位置、描述
- 描述提取：取本行描述，并最多向后追加3行续行（遇到 UVM 条目、空行或**非缩进行**停止）
- 多文件**并行解析**（`ThreadPoolExecutor`，`as_completed` 实时回调），批量场景性能优化
- 全量统计（FATAL/ERROR/WARNING 计数）基于全文所有错误行（即使 top_errors 已满仍继续扫描）
- **内存模式**：逐行流式读取（`pending` 状态机），内存占用与文件大小无关
- **`all_errors`**（v1.6 新增）：全文扫描所有 FATAL/ERROR/WARNING 的唯一 `(level, error_id)` 对，用于跨文件去重统计；仅存每种 ID 的首次出现记录，内存开销极低
- **`status`**（v1.6 新增）：单文件 PASS/FAIL 状态，`'pass'`（`UVM_ERROR==0 and UVM_FATAL==0`）或 `'fail'`

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

- 顶部汇总栏：日志总数、**PASS/FAIL 数量**（v1.6 新增，见 3.12）、**去重后的** FATAL/ERROR/WARNING 数（v1.6 新增，点击可跳转到去重详情页，见 3.13）、含未匹配错误的日志数
- 左侧文件导航列表，带彩色圆点标示严重程度（**绿色**：pass；橙色：有 WARNING；红色：有 FATAL/ERROR），含未匹配错误的日志显示"未匹配"徽章
- 右侧详情面板：错误统计、**前5条 FATAL/ERROR 列表**（每条独立展示匹配结果）
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
| 错误类型 | **是**（添加/编辑） | UVM_FATAL / UVM_ERROR / UVM_WARNING；**回写表单中显示为下拉选择框**（v1.6 新增），用户可自行修正解析到的级别 |
| 关键描述关键词 | 否 | 逗号分隔，默认预填该条错误描述前50字符 |
| 报错原因 | **是** | 根因说明，不能为空，服务端校验 |
| 所属模块 | 否 | 默认预填错误位置文件名 |
| 根因分类 | **是** | 枚举：DUT Bug / TB Bug / 用例问题 / 工具问题 / 其他问题 |
| 解决方案 | 否 | 处理建议 |
| 关联用例 | 否 | 用例名称 |
| 录入人 | 否 | 默认预填操作系统当前用户名（`getpass.getuser()`，Windows/Linux 均支持），可手动修改 |

服务端输入校验：`错误类型` 必须为合法 UVM 级别，`报错原因` 不能为空，所有字段截断至 500 字符。写入时自动追加 `录入日期`。写操作通过双层锁保证并发安全。

**去重检测**（v1.5 新增，见 3.10）：所有写入路径（新增/编辑/回写）均在执行前检查重复，发现冲突时展示警告并提供「仍要写入」强制选项。

### 3.6 报告导出

- **Excel 报告**：含"汇总"Sheet + 各日志独立 Sheet（展示前5条错误及各自匹配结果，多条命中时逐条列出「— 根因 N —」），带颜色样式
- **HTML 报告**：自包含单文件，可直接发送给无工具的人员查阅；多条命中时逐条展示；所有动态内容经 HTML 转义，防止 XSS

### 3.7 知识库管理

- 默认知识库：`error_db.xlsx`（与 exe 同目录）
- 支持在 UI 填写自定义知识库路径（可指向网络共享盘）
- 知识库不存在时自动创建含样式表头的空白文件
- **编辑条目**（v1.5 新增）：结果页每条命中条目提供「✏ 编辑」按钮，展开内联编辑表单（预填所有字段），保存后直接更新 Excel 对应行
- **删除条目**（v1.5 新增）：结果页每条命中条目提供「🗑 删除」按钮，确认后从 Excel 删除对应行，并同步更新页面显示（无需刷新）

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
- `错误类型` 和 `报错原因` 为必填项，服务端校验
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

**技术实现**：
- `/analyze` 立即返回 `{job_id}`，实际解析在后台线程（`threading.Thread`）执行
- 前端用 `EventSource` 连接 `/progress/<job_id>`，每 0.3 秒轮询一次 `_jobs[job_id]`
- 后台线程完成后将 `phase` 设置为 `'done'` 或 `'error'`，前端收到后关闭 SSE 并跳转到 `/result`

### 3.12 PASS/FAIL 统计（v1.6 新增）

顶部汇总栏新增 PASS/FAIL 统计卡：

- **PASS**：未检测到任何 `UVM_ERROR` 或 `UVM_FATAL` 的日志文件数量（绿色）
- **FAIL**：检测到至少一条 `UVM_ERROR` 或 `UVM_FATAL` 的日志文件数量（红色）
- 左侧文件导航列表的圆点新增**绿色（dot-pass）**，用于标示 PASS 文件

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
  'statistics': {'UVM_FATAL': int, 'UVM_ERROR': int, 'UVM_WARNING': int},
  'status':     str,          # 'pass'（无 ERROR/FATAL）或 'fail'（v1.6 新增）
  'top_errors': [             # 按出现顺序的前5条 FATAL/ERROR（WARNING 不在此列表）
    {
      'level':       str,     # UVM_FATAL / UVM_ERROR
      'timestamp':   str,
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
  'phase': str,   # 'pending' | 'parsing' | 'matching' | 'done' | 'error'
  'pct':   int,   # 0~100
  'msg':   str,   # 当前进度描述
  'logs':  list,  # 处理日志条目列表（每条含 time/msg）
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
| 安全 | `secure_filename` 防路径穿越；随机持久化 `secret_key`；HTML 报告全字段 `html.escape`；writeback 服务端输入校验 |
| 标准库优先 | 并发锁、报告 HTML、glob 展开均使用标准库；新功能开发优先评估标准库可行性 |
| SSE / 后台线程 | 进度推送使用 Flask SSE（`text/event-stream`），无需额外依赖；后台解析使用 `threading.Thread`，`_jobs` dict 在主线程与后台线程间共享（GIL 保护简单读写）；`sid`（session ID）在后台线程启动前提取，避免跨线程访问 Flask session |

---

## 6. 接口清单（Flask 路由）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页（上传/路径/查询/添加四模式界面） |
| POST | `/analyze` | 接收日志（上传或路径），启动后台线程，立即返回 `{job_id: str}`。表单字段：`db_path`、`logs[]`（上传模式）或 `path_mode=1` + `log_paths`（路径模式） |
| GET | `/progress/<job_id>` | SSE 进度流。每 0.3 秒推送一条 `data: {phase, pct, msg, logs, error?}\n\n`；`phase=='done'` 或 `'error'` 后关闭流（v1.6 新增） |
| GET | `/result` | 分析结果页 |
| GET | `/errors` | 去重错误详情页。查询参数 `level=UVM_FATAL\|UVM_ERROR\|UVM_WARNING`，展示该级别所有唯一错误及所在文件列表（v1.6 新增） |
| POST | `/writeback` | 写回一条知识库记录（含未匹配首次录入和已匹配补充录入）。JSON 字段含 `file_name`、`error_idx`、`level`、`reason`（必填）、`force`（可选，跳过去重）等。返回 `{success, duplicate?, conflicts?, error?}` |
| POST | `/query` | 知识库模糊查询。JSON 字段：`db_path`（可选）、`level`（可选）、`error_id`（可选，部分匹配）、`text`（可选，任意词模糊）。返回 `{entries: list, total: int}` |
| POST | `/kb/add` | 直接追加知识库条目（不依赖会话）。JSON 字段：`db_path`（可选）、`错误类型`（必填）、`报错原因`（必填）及其余知识库字段、`force`（可选）。返回 `{success, duplicate?, conflicts?, error?}` |
| POST | `/kb/update` | 编辑知识库指定行。JSON 字段：`row_idx`（Excel 行号）、`db_path`（可选）、需更新的字段、`force`（可选）。返回 `{success, duplicate?, conflicts?, error?}` |
| POST | `/kb/delete` | 删除知识库指定行。JSON 字段：`row_idx`、`db_path`（可选）。返回 `{success, error?}` |
| GET | `/export/excel` | 下载 Excel 报告 |
| GET | `/export/html` | 下载 HTML 报告 |

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
