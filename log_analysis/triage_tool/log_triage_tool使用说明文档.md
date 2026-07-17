# log_triage_tool 使用说明文档

## 1. 工具简介

`log_triage_tool` 是一个 Linux 源码版仿真日志分类分诊工具，用于批量分析 IC 验证日志，提取关键错误并和 Excel 知识库匹配，帮助验证工程师快速定位常见失败原因。

主要能力：

- 支持 UVM / VCS / Xcelium / SVA / 自定义关键字日志识别。
- 单个日志最多展示前 5 条非 warning 错误。
- 跨日志去重只使用每个出错日志的首个非 warning 错误，避免后续杂项错误污染汇总。
- 支持 Excel 知识库匹配、查询、增删改、删除撤销和备份恢复。
- 支持 Excel / HTML 报告导出。
- 可选接入 LLM，提供日志问答、相似错误、知识库质检等增强功能。

## 2. 目录结构

发布包解压后的核心目录如下：

```text
TRIAGE_TOOL/
├── README.md
└── log_analysis/triage_tool/
    ├── app.py                  # Flask 启动入口
    ├── state.py                # 全局状态和路径配置
    ├── blueprints/             # 页面和 API 路由
    ├── core/                   # 日志解析、知识库、匹配、报表、LLM 客户端
    ├── templates/              # 页面模板
    ├── static/                 # CSS 样式
    ├── packages/               # Linux 离线 wheel 依赖
    ├── error_db.xlsx           # 默认知识库
    ├── extra_patterns.json     # 额外错误关键字
    ├── pass_patterns.json      # PASS 标记
    ├── requirements.txt        # Python 依赖
    ├── install_packages.py     # 离线依赖安装脚本
    ├── DEPLOYMENT.md           # Linux 部署说明
    └── LLM_USAGE_GUIDE.md      # AI 功能说明
```

发布包不包含 `.git`、`tests`、`dist`、Windows exe、日志、缓存、密钥和开发文档。

## 3. 安装依赖

进入工具目录：

```bash
cd TRIAGE_TOOL/log_analysis/triage_tool
```

安装 Python 依赖：

```bash
python3 install_packages.py
```

如果目标机需要原生文件选择弹窗，并且 `packages/` 中包含匹配当前 Linux 发行版的 `python3-tkinter` 系统包，可以使用：

```bash
sudo python3 install_packages.py
```

说明：

- 核心分析功能只需要 Flask 和 openpyxl。
- tkinter 只用于原生文件选择弹窗。
- tkinter 缺失时，工具会提示手动输入绝对路径，不影响日志分析。

## 4. 启动工具

本机访问：

```bash
python3 app.py
```

浏览器打开：

```text
http://127.0.0.1:5000
```

局域网访问：

```bash
python3 app.py --host 0.0.0.0 --port 8080
```

同事访问：

```text
http://<服务器IP>:8080
```

后台运行示例：

```bash
nohup python3 app.py --host 0.0.0.0 --port 8080 > ~/triage.log 2>&1 &
```

## 5. 日志分析流程

### 5.1 上传模式

适合从本机浏览器上传少量日志：

1. 打开首页。
2. 选择一个或多个 `.log` 文件。
3. 选择知识库路径，默认使用工具目录下的 `error_db.xlsx`。
4. 点击开始分析。
5. 等待进度到 100%，自动进入结果页。

### 5.2 路径模式

适合日志已经在服务器或共享目录上：

1. 在路径输入框填写日志绝对路径或 glob 模式。
2. 支持多行或逗号分隔。
3. 支持 `**` 递归匹配。
4. 只会分析 `.log` 文件。

示例：

```text
/share/regress/proj_a/run_*/sim.log
/share/regress/proj_b/**/*.log
```

路径模式最多一次展开 5000 个日志文件。

## 6. 分析结果说明

结果页左侧是日志列表，右侧是当前日志详情。

顶部汇总卡片包含：

- 日志总数
- PASS 数
- FAIL 数
- 去重后的错误类型数量
- 未匹配日志数

去重逻辑：

- 单个日志详情仍展示最多前 5 条非 warning 错误。
- 跨日志去重只取每个出错日志的第一个非 warning 错误。
- warning 不进入跨日志去重列表。
- warning 数量仍在单日志统计中保留。

这样可以减少同一个日志后续连锁错误带来的噪声，更符合 IC 验证中优先看首错的习惯。

## 7. PASS / FAIL 判定

工具会全文扫描日志：

- 如果发现非 warning 错误，判定为 FAIL。
- 如果配置了 PASS 标记，则必须同时满足“没有非 warning 错误”和“找到 PASS 标记”才判定为 PASS。
- 如果日志中先出现 `JVP TEST PASSED`，后面又出现 `SVA_ERROR`，最终仍判定为 FAIL。

PASS 标记配置文件：

```text
pass_patterns.json
```

默认包含：

```json
[
  "JVP TEST PASSED"
]
```

## 8. 错误识别范围

当前内置支持：

- UVM：`UVM_ERROR`、`UVM_FATAL`、`UVM_WARNING`
- VCS：`Error-[ID]`、`Fatal-[ID]`、`Warning-[ID]`
- Xcelium：`xrun: *E,ID`、`xmsim: *SE,ID` 等
- SVA：`SVA_ERROR:`、`SVA_FATAL:`、`SVA_WARNING:`
- 自定义行首关键字：由 `extra_patterns.json` 配置

默认额外关键字示例：

```json
[
  "ERROR",
  "FATAL",
  "FAILED",
  "VIRL_MEM_WARNING",
  "JVP TEST FAILED",
  "SVA_ERROR",
  "SVA_FATAL",
  "SVA_WARNING"
]
```

## 9. 知识库使用

默认知识库文件：

```text
error_db.xlsx
```

主要字段：

- 错误类型
- 错误ID
- 关键描述关键词
- 报错原因
- 所属模块
- 根因分类
- 解决方案
- 关联用例
- 录入人
- 录入日期
- 稳定ID

匹配规则：

1. 优先按“错误类型 + 错误ID”精确匹配。
2. 如果没有错误 ID 命中，再按“关键描述关键词”匹配。
3. 关键描述关键词支持英文逗号和中文逗号分隔，所有关键词都必须在错误描述中出现。

未匹配错误可以在结果页补充原因、模块、解决方案后写回知识库。

## 10. 知识库维护

页面支持：

- 查询知识库
- 新增条目
- 更新条目
- 删除条目
- 撤销最近删除
- 查看备份
- 从备份恢复

写入知识库时，工具会做重复检查。可能重复时，前端会要求用户确认后再强制写入。

知识库写入有锁保护，支持多人或多进程场景下尽量避免 Excel 文件损坏。

## 11. 导出报告

结果页支持两种导出：

- Excel 报告
- HTML 报告

导出文件会生成到运行目录下的 `reports/` 目录。该目录属于运行产物，不进入 Git 和发布包。

## 12. AI 增强功能（可选）

不配置 LLM 时，工具以基础版运行，AI 按钮自动隐藏或不可用。

推荐生产环境用环境变量配置：

```bash
export LLM_ENDPOINT='http://内网LLM地址/v1/chat/completions'
export LLM_API_KEY='xxx'
export LLM_MODEL='qwen-max'
python3 app.py
```

也可以在页面 AI 功能区保存配置，配置会写入本机：

```text
llm_config.json
```

该文件可能包含密钥，不会进入 Git 和发布包。

AI 功能包括：

- 多条知识库命中时智能推荐
- 单日志自然语言问答
- 相似错误检索
- 批量失败模式总结
- 语义查询知识库
- 知识库质量检查和合并建议

详细说明见：

```text
LLM_USAGE_GUIDE.md
```

## 13. 发布包生成

开发机上生成 Linux 发布包：

```bash
cd /home/melo.liao/ai_tools/log_triage_tool
bash scripts/build_linux_release.sh
```

输出：

```text
release/TRIAGE_TOOL-linux-<日期>-<commit>.tar.gz
```

发布包内容会自动排除：

- `.git`
- `tests`
- `dist`
- Windows exe
- 日志文件
- 密钥文件
- 缓存目录
- 上传和报告运行目录
- 开发文档和样例大文件

## 14. 提交并发布

开发者提交代码时统一使用：

```bash
bash scripts/publish_git.sh "commit message"
```

脚本会自动：

1. 编译检查。
2. 运行全量测试。
3. 提交当前修改。
4. 基于新 commit 生成 Linux 发布包。
5. 检查发布包内容。
6. 推送当前分支到远端。

以后不要直接手工 `git push`，除非明确只是临时分支操作。

## 15. 升级迁移

升级时建议保留旧版本运行数据：

```bash
for f in error_db.xlsx llm_config.json .secret_key kb_hits.jsonl kb_hits_archive.jsonl; do
  [ -f OLD/log_analysis/triage_tool/$f ] && cp OLD/log_analysis/triage_tool/$f NEW/log_analysis/triage_tool/$f
done
```

其中：

- `error_db.xlsx`：知识库，最重要。
- `llm_config.json`：AI 配置，可能含密钥。
- `.secret_key`：Flask session 密钥，保留可减少会话失效。
- `kb_hits*.jsonl`：知识库活跃度统计。

## 16. 常见问题

### 16.1 页面打不开

检查服务是否启动：

```bash
ps -ef | grep app.py
```

检查端口是否被占用：

```bash
ss -tlnp | grep 5000
```

换端口启动：

```bash
python3 app.py --port 8080
```

### 16.2 文件选择按钮不可用

通常是 tkinter 缺失。核心功能不受影响，可以手动输入日志路径。

需要弹窗时安装系统包：

```bash
sudo yum install -y python3-tkinter
# 或 Ubuntu/Debian:
sudo apt install -y python3-tk
```

### 16.3 知识库无法写入

检查：

- `error_db.xlsx` 是否被 Excel 或其他进程打开。
- 当前用户是否有写权限。
- 是否残留 `.lock` 文件。

### 16.4 LLM 功能不可用

检查：

- endpoint 地址是否正确。
- 模型名是否和服务端一致。
- API key 是否正确。
- 内网模型服务是否能从当前机器访问。

### 16.5 分析结果 PASS 但怀疑有错误

确认日志中是否有未配置的错误格式。如果是新的行首关键字，可以在页面或 `extra_patterns.json` 中添加关键字。SVA、VCS、Xcelium 和常见 UVM 格式已内置支持。
