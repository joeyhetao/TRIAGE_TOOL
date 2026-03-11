#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
离线依赖安装脚本 — 仿真日志分类分诊工具
将 packages/ 目录中的 wheel 包离线安装到用户目录，无需联网。

用法：
    python install_packages.py

目标安装路径：/share/home/melo.liao/.local/lib/python3.6/site-packages
"""
import sys
import os
import subprocess

SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
PACKAGES_DIR     = os.path.join(SCRIPT_DIR, 'packages')
EXPECTED_PREFIX  = '/share/home/melo.liao/.local/lib/python3.6'

# 需要安装的主包（依赖由 --find-links 自动解决）
MAIN_PACKAGES = ['flask', 'openpyxl']

# packages/ 中所有包，用于预检
ALL_PACKAGES = [
    'flask', 'openpyxl', 'werkzeug', 'jinja2', 'markupsafe',
    'click', 'itsdangerous', 'colorama', 'et-xmlfile',
]


def pip_show(pkg):
    """返回 (已安装: bool, 安装路径: str)"""
    result = subprocess.run(
        [sys.executable, '-m', 'pip', 'show', pkg],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode != 0:
        return False, ''
    for line in result.stdout.decode('utf-8', errors='replace').splitlines():
        if line.startswith('Location:'):
            return True, line.split(':', 1)[1].strip()
    return True, ''


def main():
    print('=' * 60)
    print('仿真日志分类分诊工具 — 离线依赖安装脚本')
    print('=' * 60)

    # ── Python 版本检查 ────────────────────────────────────
    ver = sys.version_info
    print('\nPython 版本: {}.{}.{}'.format(ver.major, ver.minor, ver.micro))
    if not (ver.major == 3 and ver.minor == 6):
        print('警告: 当前 Python {}.{}，wheel 包针对 Python 3.6 编译，'
              '可能不兼容'.format(ver.major, ver.minor))

    # ── packages/ 目录检查 ────────────────────────────────
    if not os.path.isdir(PACKAGES_DIR):
        print('\n错误: 未找到 packages/ 目录: {}'.format(PACKAGES_DIR))
        print('请将 packages/ 文件夹与本脚本放在同一目录下。')
        sys.exit(1)

    wheels = sorted(f for f in os.listdir(PACKAGES_DIR) if f.endswith('.whl'))
    print('\npackages/ 目录共 {} 个 wheel 文件:'.format(len(wheels)))
    for w in wheels:
        print('    {}'.format(w))

    # ── 预检：已安装但不在预期路径的包 ────────────────────
    print('\n--- 预检已安装包 ---')
    warn_pkgs = []
    for pkg in ALL_PACKAGES:
        installed, location = pip_show(pkg)
        if not installed:
            print('  [ 未安装 ] {}'.format(pkg))
        elif EXPECTED_PREFIX in location:
            print('  [   OK   ] {} → {}'.format(pkg, location))
        else:
            warn_pkgs.append((pkg, location))
            print('  [ 警告   ] {} 已安装，但不在预期路径！'.format(pkg))
            print('             当前路径: {}'.format(location))
            print('             预期路径: {}'.format(EXPECTED_PREFIX))

    if warn_pkgs:
        print('\n注意：以上 {} 个包的安装位置与预期不符，'
              '可能引起版本冲突。'.format(len(warn_pkgs)))
        print('仍将继续安装到用户目录（--user），'
              '若运行时出现 ImportError 请检查 PYTHONPATH。\n')

    # ── 安装 ──────────────────────────────────────────────
    print('--- 开始安装 ---')
    cmd = [
        sys.executable, '-m', 'pip', 'install',
        '--no-index',
        '--find-links', PACKAGES_DIR,
        '--user',
    ] + MAIN_PACKAGES

    print('执行命令: {}\n'.format(' '.join(cmd)))
    ret = subprocess.call(cmd)

    if ret != 0:
        print('\n安装失败，请检查上方错误信息。')
        sys.exit(1)

    # ── 安装后验证 ────────────────────────────────────────
    print('\n--- 安装验证 ---')
    all_ok = True
    for pkg in MAIN_PACKAGES:
        installed, location = pip_show(pkg)
        if installed:
            if EXPECTED_PREFIX in location:
                print('  [   OK   ] {} → {}'.format(pkg, location))
            else:
                print('  [ 警告   ] {} 安装在: {}（非预期路径）'.format(
                    pkg, location))
                all_ok = False
        else:
            print('  [ 失败   ] {} 未能正常安装'.format(pkg))
            all_ok = False

    print('\n' + '=' * 60)
    if all_ok:
        print('安装完成！启动应用：')
        print('    python app.py')
        print('    python app.py --host 0.0.0.0 --port 5000')
    else:
        print('安装完成，但存在警告，请检查上方输出。')
    print('=' * 60)


if __name__ == '__main__':
    main()
