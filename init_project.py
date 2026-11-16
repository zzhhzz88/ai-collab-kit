#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给一个项目文件夹做协作初始化（每个项目只需要跑一次）。

    python init_project.py                 对当前文件夹
    python init_project.py D:\\code\\my-project
    python init_project.py --private       公司/团队仓库用：新建的文件都只在本地忽略，不会被 git 提交

做的事（已存在的文件一律不覆盖）：
  - 不是 git 仓库就 git init
  - 创建 AGENTS.md（Codex 读）、CLAUDE.md（只有一行 @AGENTS.md，Claude 读同一份）、PLAN.md（交接用）、
    AI_MEMORY.md（两边共享的长期记忆）
  - 把 AI_LOG.md、AI_HANDOFF.md、.turntrail/ 写进 .git/info/exclude（仅本机忽略，不改团队的 .gitignore）
"""
import argparse
import subprocess
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parent
LOCAL_FILES = ["AI_LOG.md", "AI_HANDOFF.md", ".turntrail/"]
PRIVATE_FILES = ["AGENTS.md", "CLAUDE.md", "PLAN.md", "AI_MEMORY.md"]


def git(cwd, *args):
    r = subprocess.run(["git"] + list(args), cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode, r.stdout.decode("utf-8", "replace").strip(), r.stderr.decode("utf-8", "replace").strip()


def create(path, text):
    if path.exists():
        print("已存在，跳过：%s" % path.name)
        return
    path.write_text(text, encoding="utf-8", newline="\n")
    print("已创建：%s" % path.name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", nargs="?", default=".")
    ap.add_argument("--private", action="store_true", help="新建的 AI 文件全部只在本机忽略（适合公司仓库）")
    args = ap.parse_args()

    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        sys.exit("找不到文件夹：%s" % folder)

    try:
        code, out, _ = git(folder, "rev-parse", "--show-toplevel")
    except FileNotFoundError:
        sys.exit("电脑上没有 git 命令，请先安装 Git for Windows。")
    if code != 0:
        code, _, err = git(folder, "init")
        if code != 0:
            sys.exit("git init 失败：%s" % err)
        print("这里还不是 git 仓库，已执行 git init。")
        root = folder
    else:
        root = Path(out)
        if root != folder:
            print("注意：这个文件夹在仓库 %s 里面，文件会建在仓库根目录。" % root)

    create(root / "AGENTS.md", (KIT / "templates" / "AGENTS.md").read_text(encoding="utf-8"))
    create(root / "CLAUDE.md", "@AGENTS.md\n")
    create(root / "PLAN.md", (KIT / "templates" / "PLAN.md").read_text(encoding="utf-8"))
    create(root / "AI_MEMORY.md", (KIT / "templates" / "AI_MEMORY.md").read_text(encoding="utf-8"))

    to_ignore = LOCAL_FILES + (PRIVATE_FILES if args.private else [])
    code, gitdir, _ = git(root, "rev-parse", "--git-path", "info/exclude")
    if code == 0:
        exclude = Path(gitdir) if Path(gitdir).is_absolute() else root / gitdir
        exclude.parent.mkdir(parents=True, exist_ok=True)
        old = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        have = set(line.strip() for line in old.splitlines())
        add = [f for f in to_ignore if f not in have]
        if add:
            with open(str(exclude), "a", encoding="utf-8", newline="\n") as f:
                if old and not old.endswith("\n"):
                    f.write("\n")
                f.write("\n".join(add) + "\n")
            print("已在本机忽略（.git/info/exclude）：%s" % "、".join(add))

    print("\n完成。接下来打开 AGENTS.md，把“项目简介 / 常用命令”几行改成你项目的真实内容。")


if __name__ == "__main__":
    main()
