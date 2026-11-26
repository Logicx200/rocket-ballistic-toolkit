# -*- coding: utf-8 -*-
"""维护自检：文档相对链接、图片引用、Python 语法编译、关键文件存在性。

用法（项目根目录）：python tests\\check_docs_links.py
退出码 0 = 全部通过。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

LINK_RE = re.compile(r"\]\(([^)]+)\)")

#: 顶层文档（不检查 旧版v2/旧版v3 归档，它们内部互相引用且已冻结）
DOC_DIRS = [os.path.join(ROOT, "文档")]
TOP_MD = ["README.md", "CHANGELOG.md", "交付说明_v4.md"]

SKIP_ARCHIVE = ("旧版v2", "旧版v3")


def check_links() -> list:
    problems = []
    files = [os.path.join(ROOT, f) for f in TOP_MD if os.path.exists(os.path.join(ROOT, f))]
    for d in DOC_DIRS:
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if os.path.isfile(p) and name.endswith(".md"):
                files.append(p)
    for p in files:
        rel_dir = os.path.dirname(p)
        try:
            text = open(p, encoding="utf-8").read()
        except OSError as exc:
            problems.append("%s 读取失败：%s" % (p, exc))
            continue
        for m in LINK_RE.finditer(text):
            target = m.group(1).strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = target.split("#", 1)[0].strip()
            if not target:
                continue
            # 去掉 markdown 的行内锚点/尖括号
            target = target.strip("<>")
            path = os.path.normpath(os.path.join(rel_dir, target.replace("/", os.sep)))
            if any(s in path for s in SKIP_ARCHIVE):
                continue
            if not os.path.exists(path):
                problems.append("%s → 链接目标不存在：%s" % (
                    os.path.relpath(p, ROOT), target))
    return problems


def check_compile() -> list:
    import py_compile
    import tempfile
    problems = []
    # Windows 上不能用 os.devnull 作为 cfile，用一个临时目录接收 .pyc
    tmpdir = tempfile.mkdtemp(prefix="rk_pyc_")
    for base in ("app", "engine", "tests"):
        for root, _dirs, names in os.walk(os.path.join(ROOT, base)):
            if "__pycache__" in root:
                continue
            for nm in names:
                if not nm.endswith(".py"):
                    continue
                p = os.path.join(root, nm)
                cfile = os.path.join(tmpdir, "%d.pyc" % abs(hash(p)))
                try:
                    py_compile.compile(p, doraise=True, cfile=cfile)
                except Exception as exc:  # noqa: BLE001
                    problems.append("语法错误：%s（%s）" % (os.path.relpath(p, ROOT), exc))
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    return problems


def check_keyfiles() -> list:
    need = [
        "app/main.py", "app/core/userdb.py", "app/core/summary.py",
        "app/core/config_build.py", "app/core/runner.py", "app/gui/main_window.py",
        "app/gui/tab_vehicle.py", "app/gui/dialog_rocket.py", "app/gui/dialog_orbit.py",
        "app/gui/dialog_help.py", "app/gui/help_text.py",
        "engine/scripts/export.py", "engine/scripts/qc.py",
        "文档/00_文档索引.md", "文档/01_快速开始.md", "文档/03_自定义箭型.md",
        "文档/05_输出成果清单.md", "文档/07_架构与维护.md",
        "README.md", "CHANGELOG.md", "交付说明_v4.md", "启动器.bat",
        "app/VERSION",
        "tests/test_userdb.py", "tests/test_v4_custom.py", "tests/test_summary.py",
        "tests/test_gui_headless.py",
    ]
    return [p for p in need if not os.path.exists(os.path.join(ROOT, p))]


def main() -> int:
    bad = []
    links = check_links()
    print("文档链接检查：%s" % ("通过" if not links else "%d 个问题" % len(links)))
    for x in links[:30]:
        print("  -", x)
    bad += links
    comp = check_compile()
    print("Python 语法编译：%s" % ("通过" if not comp else "%d 个问题" % len(comp)))
    for x in comp[:20]:
        print("  -", x)
    bad += comp
    miss = check_keyfiles()
    print("关键文件存在性：%s" % ("通过" if not miss else "缺少 %d 个" % len(miss)))
    for x in miss:
        print("  - 缺少：", x)
    bad += miss
    print("SELF-CHECK %s" % ("OK" if not bad else "FAILED"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
