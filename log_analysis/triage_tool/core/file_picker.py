#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立 tkinter 文件选择脚本 — 由 blueprints/kb.py 的 /pick_db_file 路由
通过 subprocess.run 调用。

为什么要拆成独立子进程：
  Linux/X11 环境下 tkinter modal dialog 在没有活跃 mainloop 时可能
  挂死，且无法 Ctrl+C 中断。直接在 Flask 请求线程里调会让请求永久
  Pending。子进程方案让父进程能用 subprocess timeout 强制兜底。

调用约定：
  argv[1] : initial_dir（可空字符串）
  stdout  : 单行 JSON {"ok": bool, "path": str, "reason": str}
            ok=True 且 path 非空 → 用户选了文件
            ok=True 且 path 为空 → 用户取消
            ok=False             → 出错（reason 描述原因）
  exit 0  : 正常返回（含用户取消）
  exit 1  : 出错
"""
import sys
import json


def main():
    initial_dir = sys.argv[1] if len(sys.argv) > 1 else ''

    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as e:
        print(json.dumps({'ok': False, 'reason': 'tkinter 不可用: {}'.format(e)},
                         ensure_ascii=False))
        sys.exit(1)

    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askopenfilename(
            parent=root,
            title='选择知识库文件',
            initialdir=initial_dir or None,
            filetypes=[('Excel 文件', '*.xlsx'), ('所有文件', '*.*')],
        )
        root.destroy()
    except Exception as e:
        print(json.dumps({'ok': False, 'reason': '弹窗失败: {}'.format(e)},
                         ensure_ascii=False))
        sys.exit(1)

    print(json.dumps({'ok': True, 'path': path or ''}, ensure_ascii=False))
    sys.exit(0)


if __name__ == '__main__':
    main()
