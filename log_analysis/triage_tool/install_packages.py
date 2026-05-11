#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线依赖安装脚本 — 仿真日志分类分诊工具

预检并安装两类依赖：
  1. pip wheel  (packages/*.whl)        — flask、openpyxl 及其传递依赖
  2. 系统包     (packages/*.deb / *.rpm) — python3-tk（提供 tkinter）

为什么 tkinter 不在 wheel 里：tkinter 是 Python 与系统 Tcl/Tk 共享库
的 C 绑定，pip 永远装不了；必须用 apt/dnf/yum 装系统包。

用法：
    python3 install_packages.py            # 仅安装当前用户能装的部分（pip wheel）
    sudo  python3 install_packages.py      # 同时尝试安装系统包（.deb/.rpm）

如何为目标机预先准备离线包（在和目标机同发行版的联网机上执行）：
    pip download flask openpyxl -d ./packages
    # Debian/Ubuntu:
    apt-get download python3-tk libtk8.6 libtcl8.6   # 版本号视目标机而定
    mv *.deb ./packages/
    # RHEL/CentOS/openEuler:
    dnf download --resolve --destdir ./packages python3-tkinter
    #   或老系统：yumdownloader --resolve --destdir ./packages python3-tkinter

注意：tkinter 系统包跨发行版和大版本不通用——目标机若是 Ubuntu 22.04，
就要在 Ubuntu 22.04 联网机上下载；CentOS 7 不能用 RHEL 9 的 .rpm。
"""
import sys
import os
import shutil
import site
import subprocess
from pathlib import Path

# 兜底：内网机 locale 常是 C/POSIX，Python 默认 stdout 编码退化为 ASCII，
# print 中文会抛 UnicodeEncodeError。强制切到 UTF-8 后再继续。
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

SCRIPT_DIR   = Path(__file__).resolve().parent
PACKAGES_DIR = SCRIPT_DIR / 'packages'

PIP_PACKAGES = ['flask', 'openpyxl']


# ── 工具函数 ──────────────────────────────────────────────

def _header(title):
    print('\n' + '=' * 60)
    print(title)
    print('=' * 60)


def _is_root():
    return hasattr(os, 'geteuid') and os.geteuid() == 0


def _detect_distro_family():
    """返回 'debian' / 'rpm' / 'unknown'。"""
    if shutil.which('dpkg') and shutil.which('apt-get'):
        return 'debian'
    if shutil.which('rpm') and (shutil.which('dnf') or shutil.which('yum')):
        return 'rpm'
    return 'unknown'


def _check_tkinter():
    try:
        import tkinter  # noqa: F401
        return True
    except ImportError:
        return False


def _pip_show(pkg):
    """返回 (已安装: bool, 安装路径: str)。"""
    res = subprocess.run([sys.executable, '-m', 'pip', 'show', pkg],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        return False, ''
    for line in res.stdout.decode('utf-8', 'replace').splitlines():
        if line.startswith('Location:'):
            return True, line.split(':', 1)[1].strip()
    return True, ''


# ── 步骤 1：tkinter 系统包安装 ────────────────────────────

def install_tkinter():
    _header('步骤 1 / 2  tkinter（系统包）')
    if _check_tkinter():
        print('  [ OK   ] tkinter 已可用')
        return True

    print('  [ 缺失 ] import tkinter 失败')
    family = _detect_distro_family()
    print('  当前系统：{}'.format(family))

    if family == 'debian':
        debs = sorted(PACKAGES_DIR.glob('*.deb'))
        if debs:
            return _install_offline_debs(debs)
        _print_install_hint_debian()
    elif family == 'rpm':
        rpms = sorted(PACKAGES_DIR.glob('*.rpm'))
        if rpms:
            return _install_offline_rpms(rpms)
        _print_install_hint_rpm()
    else:
        print('  无法识别发行版，请手动安装 tkinter：')
        print('    Debian/Ubuntu :  sudo apt install -y python3-tk')
        print('    RHEL/CentOS   :  sudo dnf install -y python3-tkinter')

    return False


def _install_offline_debs(debs):
    print('  发现 {} 个 .deb 离线包'.format(len(debs)))
    for d in debs:
        print('    {}'.format(d.name))
    if not _is_root():
        print('  [ 跳过 ] 需要 root 权限。请用 sudo 重新运行此脚本。')
        return False
    cmd = ['dpkg', '-i'] + [str(p) for p in debs]
    print('  执行：{}'.format(' '.join(cmd)))
    if subprocess.call(cmd) != 0:
        print('  dpkg 报错，尝试 apt-get install -f 修复依赖 ...')
        subprocess.call(['apt-get', 'install', '-f', '-y'])
    return _check_tkinter()


def _install_offline_rpms(rpms):
    print('  发现 {} 个 .rpm 离线包'.format(len(rpms)))
    for r in rpms:
        print('    {}'.format(r.name))
    if not _is_root():
        print('  [ 跳过 ] 需要 root 权限。请用 sudo 重新运行此脚本。')
        return False
    cmd = ['rpm', '-Uvh'] + [str(p) for p in rpms]
    print('  执行：{}'.format(' '.join(cmd)))
    subprocess.call(cmd)
    return _check_tkinter()


def _print_install_hint_debian():
    print('  packages/ 中无 .deb 离线包。两种安装方式：')
    print('    在线安装          :  sudo apt install -y python3-tk')
    print('    离线安装（推荐内网）:')
    print('      在同版本 Ubuntu/Debian 联网机上执行：')
    print('        apt-get download python3-tk libtk8.6 libtcl8.6')
    print('        cp *.deb <项目>/log_analysis/triage_tool/packages/')
    print('      然后在内网机：sudo python3 install_packages.py')


def _print_install_hint_rpm():
    print('  packages/ 中无 .rpm 离线包。两种安装方式：')
    print('    在线安装          :  sudo dnf install -y python3-tkinter')
    print('    离线安装（推荐内网）:')
    print('      在同版本 RHEL/CentOS 联网机上执行：')
    print('        dnf download --resolve --destdir ./packages python3-tkinter')
    print('      然后在内网机：sudo python3 install_packages.py')


# ── 步骤 2：pip wheel 离线安装 ────────────────────────────

def install_pip_wheels():
    _header('步骤 2 / 2  pip wheel（flask / openpyxl）')
    wheels = sorted(PACKAGES_DIR.glob('*.whl'))
    if not wheels:
        print('  packages/ 中无 .whl 文件，跳过。')
        return False

    print('  Python   : {}.{}.{}'.format(*sys.version_info[:3]))
    print('  USER_SITE: {}'.format(site.USER_SITE))
    print('  发现 {} 个 wheel：'.format(len(wheels)))
    for w in wheels:
        print('    {}'.format(w.name))

    cmd = [sys.executable, '-m', 'pip', 'install',
           '--no-index',
           '--find-links', str(PACKAGES_DIR),
           '--user'] + PIP_PACKAGES
    print('\n  执行：{}'.format(' '.join(cmd)))
    if subprocess.call(cmd) != 0:
        print('  pip 安装失败，请检查上方错误信息。')
        print('  常见原因：wheel 文件 cpython 版本与当前 Python 不匹配。')
        print('    当前 Python: cp{}{}'.format(*sys.version_info[:2]))
        print('    需要在同版本 Python 联网机上重新 pip download。')
        return False

    print('\n  验证：')
    all_ok = True
    for pkg in PIP_PACKAGES:
        installed, location = _pip_show(pkg)
        marker = '  [ OK ]' if installed else '  [FAIL]'
        print('  {} {} → {}'.format(marker, pkg, location or '(未找到)'))
        all_ok = all_ok and installed
    return all_ok


# ── main ─────────────────────────────────────────────────

def main():
    _header('仿真日志分类分诊工具 — 离线依赖安装脚本')
    print('Python   : {}'.format(sys.version.replace('\n', ' ')))
    print('Packages : {}'.format(PACKAGES_DIR))
    print('Root     : {}'.format('yes' if _is_root() else 'no（系统包安装需 sudo）'))

    if not PACKAGES_DIR.is_dir():
        print('\n错误：packages/ 目录不存在，无法继续。')
        sys.exit(1)

    tk_ok  = install_tkinter()
    pip_ok = install_pip_wheels()

    _header('安装总结')
    print('  tkinter        : {}'.format('OK' if tk_ok else '缺失（按上方提示手动安装）'))
    print('  flask/openpyxl : {}'.format('OK' if pip_ok else 'FAIL'))

    if tk_ok and pip_ok:
        print('\n全部依赖就位。启动应用：')
        print('  python3 app.py')
    elif pip_ok and not tk_ok:
        print('\npip 依赖 OK；tkinter 缺失不阻塞核心功能。')
        print('"选择文件" 按钮会自动 fallback 到提示用户手输绝对路径。')
    sys.exit(0 if pip_ok else 1)


if __name__ == '__main__':
    main()
