# GitHub 使用指南

## 基础概念：网页账号 vs 本地 Git

**本地 Git** 和 **GitHub 网页** 是两个独立的东西，但通过"凭证"联系起来：

```
本地 Git（你电脑上的工具）
    ↕  通过 HTTPS 或 SSH 认证
GitHub（云端代码托管平台）
```

- **本地 Git**：负责版本控制，记录代码变更历史
- **GitHub**：相当于"云端仓库"，存储和共享代码

---

## 基本工作流程

```
GitHub 网页创建仓库
        ↓
git clone（下载到本地）
        ↓
本地编辑代码
        ↓
git add + git commit（保存到本地历史）
        ↓
git push（上传到 GitHub）
```

---

## 快速上手步骤

### 第一步：配置本地身份

```bash
git config --global user.name "你的名字"
git config --global user.email "你的GitHub邮箱"
```

### 第二步：在 GitHub 网页创建仓库

登录 github.com → New repository → 填写名称 → Create

### 第三步：克隆到本地

```bash
git clone https://github.com/你的用户名/仓库名.git
cd 仓库名
```

### 第四步：修改代码后提交上传

```bash
git add .                        # 暂存所有修改
git commit -m "描述这次改了什么"  # 提交到本地
git push                         # 推送到 GitHub
```

---

## 认证方式（让本地能连接到你的 GitHub 账号）

| 方式 | 说明 |
|------|------|
| HTTPS + Token | 简单，GitHub 已不支持密码，需生成 Personal Access Token |
| SSH Key | 一次配置永久使用，推荐 |

### HTTPS Token 方式

1. GitHub → Settings → Developer settings → Personal access tokens → Generate new token
2. `git push` 时用 token 代替密码

### SSH 方式（推荐）

```bash
ssh-keygen -t ed25519 -C "你的邮箱"   # 生成密钥
cat ~/.ssh/id_ed25519.pub              # 复制公钥内容
```

然后粘贴到：GitHub → Settings → SSH and GPG keys → New SSH key

---

## 切换 GitHub 账号

### 查看 / 修改当前配置

```bash
git config --global user.name
git config --global user.email

git config --global user.name "新用户名"
git config --global user.email "新邮箱@example.com"
```

### 清除 Windows 凭证（重新登录）

控制面板 → 凭据管理器 → Windows 凭据 → 找到 `git:https://github.com` → 删除

### GitHub CLI 切换

```bash
gh auth logout
gh auth login
```

### SSH 多账号配置（长期推荐）

编辑 `~/.ssh/config`：

```
Host github-work
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_rsa_work

Host github-personal
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_rsa_personal
```

使用时指定对应 Host：

```bash
git remote set-url origin git@github-personal:username/repo.git
git remote set-url origin git@github-work:company/repo.git
```

---

## 常用命令速查

```bash
git status              # 查看当前状态
git log --oneline       # 查看提交历史（简洁）
git pull                # 拉取远端最新代码
git branch 分支名       # 创建新分支
git checkout 分支名     # 切换分支
git diff                # 查看未暂存的修改
git stash               # 临时保存未提交的修改
git stash pop           # 恢复临时保存的修改
```

---

## 总结

> **网页账号** = 身份认证 + 云端存储
> **本地 Git** = 操作工具
> 两者通过 **HTTPS Token** 或 **SSH 密钥** 绑定连接。
