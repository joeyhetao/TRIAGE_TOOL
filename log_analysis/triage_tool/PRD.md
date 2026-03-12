# 仿真日志分类分诊工具 — 产品需求文档（PRD）

**文档版本**：v1.2
**基准代码版本**：2026-03-12
**适用范围**：功能增改、需求评审、开发参考

---

## 1. 产品定位

面向芯片验证工程师的内网桌面工具，用于批量解析 UVM 仿真日志、自动匹配已知错误知识库、沉淀和复用排查经验，减少重复定位时间。

**部署形态**：
- **Windows**：单个 `triage_tool.exe`，双击运行，浏览器访问本地 Web 界面
- **Linux**：`python app.py` 直接启动，适用于在仿真服务器上就地分析本地日志

知识库文件（`error_db.xlsx`）支持放置于网络共享盘供多人共用。

---

## 2. 用户与使用场景

| 角色 | 使用场景 |
|---|---|
| 验证工程师 | 上传或指定本地 log，查看自动分析结果，快速定位前几条错误 |
| 资深工程师 | 将新错误根因写回知识库，积累团队经验 |
| 团队负责人 | 导出分析报告，归档或发送给相关人员 |

---

## 3. 当前功能（v1.2）

### 3.1 日志输入

支持两种输入模式，通过首页 Tab 切换：

**模式一：上传文件**
- 拖拽或点击选择 `.log` 文件，可多选批量上传
- 文件通过浏览器 HTTP multipart 上传至服务器 `uploads/` 目录后解析
- 单文件大小限制：10 GB

**模式二：指定路径**（v1.2 新增）
- 在文本框中直接填写服务器本地文件路径，无需上传拷贝
- 支持 glob 通配符（`*`、`**` 递归），每行一条路径或逗号分隔
- 服务端展开 glob、过滤非 `.log` 文件，单次最多 100 个文件
- 适用于日志在仿真服务器本地的场景，省去大文件传输开销

### 3.2 日志解析

**UVM 日志格式**：
```
UVM_ERROR /path/file.sv(142) @ 1000ns: uvm_test_top.env [ID] message
```

- 解析每个日志文件，提取所有 `UVM_FATAL` / `UVM_ERROR` / `UVM_WARNING` 条目
- **前5条错误**（`top_errors`）：按出现顺序提取最多5条，供展示和知识库匹配
- 每条错误记录包含：级别、时间戳、错误ID、文件位置、描述
- 描述提取：取本行描述，并最多向后追加3行续行（遇到 UVM 条目或空行停止）
- 多文件**并行解析**（`ThreadPoolExecutor`），批量场景性能优化
- 全量统计（FATAL/ERROR/WARNING 计数）仍基于全部错误行

### 3.3 知识库匹配

两阶段匹配，对每个日志的 **前5条错误各自独立执行**：

1. **精确匹配**：错误ID（忽略大小写）+ 错误类型完全一致
2. **关键词匹配**：`关键描述关键词`（中英文逗号分隔，AND 逻辑）全部出现在描述中

匹配结果状态：`matched`（命中）/ `unmatched`（未匹配）/ `no_error`（无报错）

**汇总状态**（`r.match`）：有任意条 unmatched → `unmatched`；全部命中 → `matched`。

### 3.4 结果展示

- 顶部汇总栏：日志总数、FATAL/ERROR/WARNING 总计、含未匹配错误的日志数
- 左侧文件导航列表，带彩色圆点标示严重程度，含未匹配错误的日志显示"未匹配"徽章
- 右侧详情面板：错误统计、**前5条错误列表**（每条独立展示匹配结果）

### 3.5 知识库回写

对未匹配错误，在结果页直接填写根因信息并写回知识库。**每条未匹配错误均有独立回写表单**（v1.2 变更，原来只有整个日志一个表单）：

| 字段 | 必填 | 说明 |
|---|---|---|
| 关键描述关键词 | 否 | 逗号分隔，默认预填该条错误描述前50字符 |
| 报错原因 | **是** | 根因说明，不能为空，服务端校验 |
| 所属模块 | 否 | 默认预填错误位置文件名 |
| 根因分类 | **是** | 枚举：DUT Bug / TB Bug / 用例问题 / 环境问题 |
| 解决方案 | 否 | 处理建议 |
| 关联用例 | 否 | 用例名称 |
| 录入人 | 否 | 姓名 |

服务端输入校验：`level` 必须为合法 UVM 级别，`reason` 不能为空，所有字段截断至 500 字符。写入时自动追加 `录入日期`。写操作通过双层锁保证并发安全。

### 3.6 报告导出

- **Excel 报告**：含"汇总"Sheet + 各日志独立 Sheet（展示前5条错误及各自匹配结果），带颜色样式
- **HTML 报告**：自包含单文件，可直接发送给无工具的人员查阅；所有动态内容经 HTML 转义，防止 XSS

### 3.7 知识库管理

- 默认知识库：`error_db.xlsx`（与 exe 同目录）
- 支持在 UI 填写自定义知识库路径（可指向网络共享盘）
- 知识库不存在时自动创建含样式表头的空白文件

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
| 根因分类 | 字符串 | DUT Bug / TB Bug / 用例问题 / 环境问题 |
| 解决方案 | 字符串 | 处理建议 |
| 关联用例 | 字符串 | 相关测试用例名 |
| 录入人 | 字符串 | 姓名 |
| 录入日期 | 字符串 | `YYYY-MM-DD`，写入时自动生成 |

### 4.2 内存数据结构（单次分析会话，v1.2）

```python
{
  'file':       str,          # 显示文件名（basename）
  'filepath':   str,          # 服务器上的完整路径
  'statistics': {'UVM_FATAL': int, 'UVM_ERROR': int, 'UVM_WARNING': int},
  'top_errors': [             # 按出现顺序的前5条错误，run_match() 追加各自 match 字段
    {
      'level':       str,     # UVM_FATAL / UVM_ERROR / UVM_WARNING
      'timestamp':   str,
      'error_id':    str,
      'location':    str,
      'description': str,
      'match': {
        'status':   str,      # matched / unmatched / no_error
        'match_by': str,      # error_id / keywords / manual（仅 matched）
        'entry':    dict | None,
      }
    },
    ...                       # 最多5条
  ],
  'match': {                  # 汇总状态：有任意 unmatched → unmatched；全部命中 → matched
    'status':   str,
    'match_by': str,
    'entry':    dict | None,
  }
}
```

会话数据存于模块级 `_store` dict，TTL 为 2 小时，过期后自动清理。

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

---

## 6. 接口清单（Flask 路由）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页（上传/路径选择界面） |
| POST | `/analyze` | 接收日志（上传或路径），返回 `{redirect: '/result'}`。表单字段：`db_path`、`logs[]`（上传模式）或 `path_mode=1` + `log_paths`（路径模式） |
| GET | `/result` | 分析结果页 |
| POST | `/writeback` | 写回一条知识库记录。JSON 字段含 `file_name`、`error_idx`（错误在 top_errors 中的下标）、`level`、`reason`（必填）等。返回 `{success: bool, error?: str}` |
| GET | `/export/excel` | 下载 Excel 报告 |
| GET | `/export/html` | 下载 HTML 报告 |

---

## 7. 已知限制与待决策项

| 编号 | 描述 | 当前状态 |
|---|---|---|
| L-01 | 会话数据存内存，重启 exe 后分析结果丢失，无法回溯历史 | 已知，设计如此 |
| L-02 | ~~仅取"首错"匹配，多条错误的全量匹配暂不支持~~ | **v1.2 已解决**：改为提取前5条错误逐一匹配 |
| L-03 | 知识库只能追加，不支持在 UI 中编辑或删除已有条目 | 待评估 |
| L-04 | ~~上传文件永久保存在 `uploads/` 目录，无自动清理机制~~ | **v1.2 已解决**：解析完成后立即删除；启动时清理24小时以上旧文件作为兜底 |
| L-05 | `_FileLock` 超时（默认15秒）时写操作失败，前端提示错误但不重试 | 待评估 |
| L-06 | 根因分类为硬编码枚举，新增类别需改代码 | 待评估 |
| L-07 | 路径模式下，同名文件（来自不同目录）在结果页仅以 basename 区分，可能产生混淆 | 已知，轻微 |
| L-08 | ~~上传文件清理仅在启动时执行一次，长期不重启时文件持续累积~~ | **v1.2 已解决**：改为解析完成后立即删除，不再依赖重启触发清理 |

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
