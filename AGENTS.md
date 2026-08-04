# TRIAGE_TOOL 操作范围

本仓库只包含 `TRIAGE_TOOL`。涉及修改、构建、提交或推送时，目标必须是：

- 仓库根：`log_triage_tool`
- 工具源码：`log_analysis/triage_tool`
- Git 远程：`joeyhetao/TRIAGE_TOOL.git`
- 推送分支：当前检出的分支

不得在 `/home/melo.liao/ai_tools/xlog` 或其他项目中执行本仓库任务。发布或推送必须使用 `scripts/publish_git.sh`；该脚本会调用 `scripts/verify_triage_scope.sh` 验证仓库、远程和分支。
