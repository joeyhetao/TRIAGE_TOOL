# Linux 源码版部署手册

本文只覆盖 Linux 源码运行方式。Windows exe / PyInstaller 二进制分发已从发布版移除。

## 1. 解压发布包

```bash
tar xzf TRIAGE_TOOL-linux-<version>.tar.gz
cd TRIAGE_TOOL/log_analysis/triage_tool
```

## 2. 安装依赖

联网或已带离线 wheels 的环境：

```bash
python3 install_packages.py
```

如果 `packages/` 中包含 `python3-tkinter` 对应的 `.rpm` / `.deb`，并希望启用原生文件选择弹窗，请使用 sudo：

```bash
sudo python3 install_packages.py
```

核心功能只依赖 Flask 和 openpyxl。tkinter 缺失时，文件选择按钮会退化为手工输入路径，不影响日志分析。

## 3. 启动服务

本机访问：

```bash
python3 app.py
```

局域网访问：

```bash
python3 app.py --host 0.0.0.0 --port 8080
```

浏览器打开：

```text
http://127.0.0.1:5000
```

## 4. 可选 LLM 配置

生产环境推荐使用环境变量，不把 API key 写入文件：

```bash
export LLM_ENDPOINT='http://内网LLM地址/v1/chat/completions'
export LLM_API_KEY='xxx'
export LLM_MODEL='qwen-max'
python3 app.py
```

也可以启动后在页面中保存配置，配置会写入本机 `llm_config.json`。该文件被 `.gitignore` 和发布脚本排除。

## 5. 后台运行

```bash
nohup python3 app.py --host 0.0.0.0 --port 8080 > ~/triage.log 2>&1 &
```

或使用 tmux：

```bash
tmux new -s triage 'python3 app.py --host 0.0.0.0 --port 8080'
```

## 6. 升级

新版本解压后，按需从旧目录复制运行数据：

```bash
for f in error_db.xlsx llm_config.json .secret_key kb_hits.jsonl kb_hits_archive.jsonl; do
  [ -f OLD/log_analysis/triage_tool/$f ] && cp OLD/log_analysis/triage_tool/$f NEW/log_analysis/triage_tool/$f
done
```

`error_db.xlsx` 是知识库；`kb_hits*.jsonl` 是 KB 活跃度历史；`llm_config.json` 可能包含密钥，请按本机安全策略管理。

## 7. 发布包内容

发布包包含：

- Flask 源码：`app.py`、`state.py`、`blueprints/`、`core/`
- 页面资源：`templates/`、`static/`
- 默认配置和知识库：`error_db.xlsx`、`extra_patterns.json`、`pass_patterns.json`
- Linux 离线 wheels：`packages/`
- 安装和使用文档：`install_packages.py`、`requirements.txt`、`DEPLOYMENT.md`、`LLM_USAGE_GUIDE.md`

发布包不包含：`.git`、`tests/`、`dist/`、Windows exe、日志、缓存、密钥、开发文档和样例大日志。
