# 内网部署手册

适用于「源码 + 离线 wheels」分发模式：每位工程师在自己的 Linux 桌面机上跑 Flask 服务，浏览器（Firefox）与服务端同机访问。

PyInstaller 二进制分发不在本文范围。

---

## 第 1 步：摸清目标机环境

让一位内网工程师跑这几条，把结果发给打包人——决定后续所有"哪个版本"问题：

```bash
cat /etc/os-release        # ID + VERSION_ID（决定 .deb 还是 .rpm）
python3 --version          # 决定 pip wheel 的 cp 版本
ldd --version | head -1    # glibc 版本
echo $DISPLAY              # 应非空（GUI 桌面环境）
firefox --version || firefox-esr --version
```

---

## 第 2 步：在「同发行版 + 同 Python 版本」的联网机准备 packages/

> **关键约束**：tkinter 的 `.deb`/`.rpm` 跨发行版/大版本绝对不通用；pip wheel 按 Python 版本编译。Ubuntu 22.04 cp310 的包**不能**装到 Ubuntu 20.04 cp38 或 CentOS 7。

最便利：用 Docker 起一台和内网完全一致的镜像：

```bash
docker run -it --rm -v "$PWD/packages:/work/packages" ubuntu:22.04 bash
# 或 centos:7 / rockylinux:8 / 任何与目标匹配的官方镜像
```

进容器后：

**Debian/Ubuntu 系**：
```bash
apt update && apt install -y python3-pip
cd /work
pip3 download flask openpyxl -d packages/                          # *.whl
cd packages
apt-get download python3-tk libtk8.6 libtcl8.6 libxft2 libxss1     # *.deb
# 注意 tk/tcl 大版本号要匹配目标系统（22.04 是 8.6）
```

**RHEL/CentOS 系**：
```bash
yum install -y python3-pip yum-utils
cd /work
pip3 download flask openpyxl -d packages/
yumdownloader --resolve --destdir packages/ python3-tkinter
# 或新系统：dnf download --resolve --destdir packages/ python3-tkinter
```

确认 `ls packages/` 同时有 `*.whl` 和 `*.deb`（或 `*.rpm`）。

---

## 第 3 步：打包项目

在项目根目录：

```bash
tar czf /tmp/TRIAGE_TOOL.tar.gz \
    --exclude='__pycache__' --exclude='build' --exclude='dist' \
    --exclude='.venv' --exclude='*.pyc' --exclude='.git' \
    -C $(dirname $PWD) $(basename $PWD)
```

`.gitignore` 已经把 `.secret_key`、`llm_config.json` 排除——**好事**：每台机器用自己生成的，避免敏感信息扩散。

> `dist/` 在我们的项目里被保留入 git（Windows/Linux 双系统二进制存放点）。源码模式部署不需要它，可以加进上面的 `--exclude`。

---

## 第 4 步：拷到内网

任选一种：U 盘 / 共享盘 / 跳板 scp。**拷一个 `.tar.gz` 文件即可**。

---

## 第 5 步：内网机首次安装（**必须 sudo**）

```bash
cd ~ && tar xzf /path/to/TRIAGE_TOOL.tar.gz
cd TRIAGE_TOOL/log_analysis/triage_tool

# 校验 wheel 跟本机 Python 版本匹配
python3 --version
ls packages/*.whl | head -3   # cp 版本应一致

# 一键装（先尝试 sudo 装系统包，再 pip --user 装 wheel）
sudo python3 install_packages.py
```

为什么必须 `sudo`：

| 文件类型 | 装到哪 | 谁有权限写 |
|---|---|---|
| `*.whl` (flask, openpyxl) | `~/.local/lib/...` 用户目录 | 当前用户即可 |
| `*.deb` / `*.rpm` (python3-tk) | `/usr/lib/...` 系统目录 | **必须 root** |

`install_packages.py` 在 packages/ 里看到什么类型就装什么，sudo 不是为 wheel 加的，是为 .deb/.rpm 加的。如果不带 sudo 也能跑——脚本会跳过系统包安装并提示，但 tkinter 弹窗"选择文件"功能不可用（fallback 到手输路径）。

---

## 第 6 步：（可选）配置 LLM

二选一：

**环境变量方式（推荐生产，不落地敏感数据）**：
```bash
export LLM_ENDPOINT='http://内网LLM地址/v1/chat/completions'
export LLM_API_KEY='xxx'
export LLM_MODEL='qwen-max'
python3 app.py
```

**UI 方式**：启动后浏览器打开 → 「🤖 AI 功能」 Tab → 填写 → 保存（写入 `llm_config.json`，文件权限 0600）。

不需要 AI 功能就跳过，主页所有 AI 按钮自动隐藏（基础版模式）。

---

## 第 7 步：启动 + 验证

```bash
python3 app.py                          # 默认 127.0.0.1:5000，仅本机
python3 app.py --host 0.0.0.0           # 让局域网/同事访问
python3 app.py --host 0.0.0.0 --port 8080   # 换端口
```

Firefox 打开 `http://127.0.0.1:5000`，逐项确认：

1. 主页加载、中文不显示豆腐块 → 字体 OK
2. 点「选择文件」→ 30 秒内 GTK 文件对话框弹出 → 选 `error_db.xlsx` → 输入框自动填**绝对路径** → 30s 仍未交互应 fallback 弹 alert 提示手输
3. 上传一个 `.log` → 点「开始分析」→ 进度条走到 100% → 看到分析结果
4. 在「查询知识库」面板查询 → 返回结果

任意一步不通，参考最后一节《常见坑》。

---

## 后台守护（断 SSH 后服务还活着）

```bash
# 简单 nohup
nohup python3 app.py > ~/triage.log 2>&1 &

# 或 tmux（推荐，能 attach 看实时日志）
tmux new -s triage 'python3 app.py'        # 启动
# Ctrl+B 再 D：脱离会话（服务继续跑）
tmux attach -t triage                       # 重连
```

---

## 升级新版本到内网

```bash
cd ~ && tar xzf TRIAGE_TOOL_NEW.tar.gz
# 保留 KB 数据和 session 密钥（不让用户掉登录）
cp TRIAGE_TOOL/log_analysis/triage_tool/error_db.xlsx \
   TRIAGE_TOOL_NEW/log_analysis/triage_tool/error_db.xlsx
cp TRIAGE_TOOL/log_analysis/triage_tool/.secret_key \
   TRIAGE_TOOL_NEW/log_analysis/triage_tool/.secret_key 2>/dev/null

# 重命名切换
mv TRIAGE_TOOL TRIAGE_TOOL.bak.$(date +%Y%m%d)
mv TRIAGE_TOOL_NEW TRIAGE_TOOL

# 重启服务（依赖一般不需重装，除非 requirements.txt 改了）
```

---

## 常见坑

| 现象 | 原因 | 解决 |
|---|---|---|
| `install_packages.py` 报 `is not a supported wheel on this platform` | 联网机 Python 版本和内网不匹配 | 第 2 步用和内网**完全相同** Python 版本的容器/机器重新下 |
| Firefox 主页中文显示豆腐块 | 系统中文字体缺失 | Ubuntu：`sudo apt install fonts-noto-cjk`<br>RHEL：`sudo dnf install wqy-zenhei-fonts dejavu-sans-fonts` |
| 「选择文件」按钮点了 30 秒后 alert 提示手输 | 桌面 GUI 异常 / `DISPLAY` 未设 / X server 不通 | 在终端跑 `python3 -c 'import tkinter; tkinter.Tk().mainloop()'` 验证；fallback 路径手输绝对路径也能用 |
| KB Excel 操作卡 15 秒后报错 | 残留 `error_db.xlsx.lock` | `rm error_db.xlsx.lock`；详见根 [CLAUDE.md](../../CLAUDE.md) "Concurrent write safety" |
| `--host 0.0.0.0` 同事访问不到 | 防火墙阻塞 5000 | `sudo firewall-cmd --add-port=5000/tcp --permanent && sudo firewall-cmd --reload`<br>或 `sudo iptables -I INPUT -p tcp --dport 5000 -j ACCEPT` |
| 端口 5000 被占 | 已有进程 | `ss -tlnp \| grep :5000` 看占用；`fuser -k 5000/tcp` 释放；或 `--port 8080` |
| SELinux 阻止 Flask 写 `uploads/` | RHEL 默认 enforcing | 临时：`sudo setenforce 0`；永久：调 SELinux context 或 `--user` 模式跑在 home 下 |

---

## 敏感文件处理（**必读**）

| 文件 | 是否随分发包过去 | 处理方式 |
|---|---|---|
| `.secret_key` (32 字节随机) | ❌ 不带（已 .gitignore） | 每台机器首次启动 `app.py` 时自动生成，权限自动 0600 |
| `llm_config.json` (含 API Key) | ❌ 不带（已 .gitignore） | 每台机器自己用 UI 或环境变量配置 |
| `error_db.xlsx` (KB 数据) | ✓ 带初始模板 | 升级时**保留旧文件**，不要覆盖，否则丢历史录入 |
| `extra_patterns.json` / `pass_patterns.json` | ✓ 带（运行配置） | 升级时根据需要保留或覆盖 |

---

## 参考

- 项目架构与开发须知：[../../CLAUDE.md](../../CLAUDE.md)
- 产品需求与功能边界：[PRD.md](PRD.md)
- 历史 bug 与根因：[BUGLOG.md](BUGLOG.md)
- LLM 功能详细配置：[LLM_USAGE_GUIDE.md](LLM_USAGE_GUIDE.md)
- 离线安装脚本本身：[install_packages.py](install_packages.py)
