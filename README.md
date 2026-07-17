# TRIAGE_TOOL

Linux 源码版仿真日志分类分诊工具。

## 功能

- 批量解析 UVM / VCS / Xcelium / SVA / 自定义关键字日志。
- 每个日志展示最多 5 条非 warning 错误，首个非 warning 错误用于跨日志去重汇总。
- 使用 Excel 知识库 `error_db.xlsx` 做错误 ID / 关键词匹配。
- 支持知识库维护、Excel / HTML 导出、可选 LLM 增强能力。

## 快速启动

```bash
cd log_analysis/triage_tool
python3 install_packages.py
python3 app.py
```

浏览器访问：

```text
http://127.0.0.1:5000
```

局域网访问示例：

```bash
python3 app.py --host 0.0.0.0 --port 8080
```

## 生成发布包

在仓库根目录执行：

```bash
bash scripts/build_linux_release.sh
```

产物位于：

```text
release/TRIAGE_TOOL-linux-<version>.tar.gz
```

发布包只包含 Linux 运行所需源码、模板、静态资源、默认配置、默认知识库、离线 wheels 和部署说明，不包含 `.git`、测试、Windows exe、日志、密钥或开发文档。

详细部署步骤见 `log_analysis/triage_tool/DEPLOYMENT.md`。
