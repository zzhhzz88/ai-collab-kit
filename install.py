#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai-collab-kit 安装脚本：一次装好，之后所有项目里的 Claude Code 和 Codex 都会
自动留痕、遵守同一套协作约定，并能在一方额度用完后由另一方接着做。

    python install.py                  安装（含 Turntrail）
    python install.py --dry-run        只显示会改哪些文件，不真的改
    python install.py --uninstall      卸载（只删掉本工具加的内容，不动你原有的配置）
    python install.py --only claude    只装 Claude 那一半（或 --only codex）
    python install.py --no-turntrail   不安装 Turntrail（接力功能将不可用）

做的事（改之前都会先备份成 *.bak-ai-collab-时间）：
  1. 把 hooks/ 里的 log_event.py、awake_hook.py、doctor.py 复制到 ~/.ai-collab/
  2. 在 Claude 的 settings.json、Codex 的 hooks.json 里加 hook：
     发消息时拍快照 + 防睡眠 + 新会话注入项目简报 + 撞车提醒；回答结束时写 AI_LOG.md；
     API 错误/额度用完（Claude StopFailure、Codex PostModelRequest 失败）时生成 AI_HANDOFF.md；
     Codex 中断（Interrupt）时释放防睡眠
  3. 在 ~/.claude/CLAUDE.md 和 ~/.codex/AGENTS.md 末尾加一段“协作约定”
  4. 安装 skill：ask-codex / ask-claude（互相只读审查）、resume-other-agent（接力）
  5. 安装 Turntrail 命令行（从上游 GitHub 克隆到 ~/.ai-collab/turntrail 并 npm link），
     并让 git 全局忽略 .turntrail/
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

KIT = Path(__file__).resolve().parent
HOME = Path.home()
DEST = HOME / ".ai-collab"   # log_event.py / awake_hook.py 里也写死了这个位置
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (HOME / ".claude"))
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex"))
AGENTS_SKILLS = HOME / ".agents" / "skills"
HOOK_FILES = ["log_event.py", "awake_hook.py", "doctor.py"]
BEGIN = "<!-- ai-collab:begin -->"
END = "<!-- ai-collab:end -->"
STAMP = time.strftime("%Y%m%d-%H%M%S")
SKILL_MARK = ".ai-collab"

TURNTRAIL_REPO = "https://github.com/yagyaanshK/turntrail.git"
TURNTRAIL_TAG = "v0.18.31"   # 已验证可用的版本
TURNTRAIL_DIR = DEST / "turntrail"
GLOBAL_IGNORE = DEST / "gitignore_global"
IGNORE_LINE = ".turntrail/"

# (事件名, 脚本, log_event 的 --event, 超时秒)
CLAUDE_HOOKS = [("UserPromptSubmit", "log_event.py", "start", 15),
                ("Stop", "log_event.py", "stop", 15),
                ("StopFailure", "log_event.py", "fail", 15)]       # API 错误（含额度用完）→ 交接
CODEX_HOOKS = [("UserPromptSubmit", "log_event.py", "start", 15),
               ("Stop", "log_event.py", "stop", 15),
               ("Interrupt", "awake_hook.py", "stop", 2),          # Codex 限制 Interrupt 最多 3 秒
               ("PostModelRequest", "log_event.py", "modelfail", 5)]  # 模型请求失败 → 更新交接

DRY = False


def say(msg):
    print(("[预览] " if DRY else "") + msg)


def backup(path):
    if path.exists() and not DRY:
        shutil.copy2(str(path), str(path) + ".bak-ai-collab-" + STAMP)


def run(cmd, cwd=None, timeout=600):
    """运行命令，返回 (returncode, 输出)。Windows 下 npm/git 等需要 shell=True 才能找到 .cmd。"""
    try:
        r = subprocess.run(cmd, cwd=None if cwd is None else str(cwd), shell=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return r.returncode, r.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        return 1, str(e)


def works(cmd):
    return run(cmd + ' -c "import sys"', timeout=15)[0] == 0


def pick_python():
    """hook 里要用的 python 命令。优先用 PATH 里能直接敲的名字（避免 PowerShell 对带引号路径的解析问题）。"""
    for cand in ("python", "py -3", "python3"):
        if works(cand):
            return cand
    exe = Path(sys.executable).as_posix()
    print("警告：PATH 里找不到可用的 python，hook 将使用完整路径：%s（若路径含空格，在 PowerShell 下可能失败）" % exe)
    return '"%s"' % exe if " " in exe else exe


def hook_command(py, script, agent, event):
    path = (DEST / script).as_posix()  # 用正斜杠，bash 和 PowerShell 都能识别
    return '%s "%s" --agent %s --event %s' % (py, path, agent, event)


def is_ours(command):
    command = str(command)
    return any(name in command for name in HOOK_FILES) and ".ai-collab" in command


def load_json(path):
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError as e:
        sys.exit("错误：%s 不是合法的 JSON（%s）。为了不弄坏它，安装已中止，请先修好或备份这个文件。" % (path, e))
    if not isinstance(data, dict):
        sys.exit("错误：%s 的顶层不是对象，安装已中止。" % path)
    return data


def strip_ours(groups):
    """去掉 groups 里本工具以前加的条目，其余原样保留。"""
    kept = []
    for g in groups:
        if "hooks" not in g:
            kept.append(g)
            continue
        hooks = [h for h in g.get("hooks", []) if not is_ours(h.get("command", ""))]
        if hooks:
            g = dict(g)
            g["hooks"] = hooks
            kept.append(g)
    return kept


def set_hooks(path, agent, spec, py, remove=False):
    data = load_json(path)
    hooks = data.setdefault("hooks", {})
    # 先把所有事件里本工具的旧条目去掉（包括旧版本装在别的事件上的）
    for event_name in list(hooks):
        groups = strip_ours(hooks.get(event_name) or [])
        if groups:
            hooks[event_name] = groups
        else:
            hooks.pop(event_name)
    if not remove:
        for event_name, script, ev, timeout in spec:
            hooks.setdefault(event_name, []).append(
                {"hooks": [{"type": "command", "command": hook_command(py, script, agent, ev), "timeout": timeout}]})
    if not hooks:
        data.pop("hooks", None)
    say("%s hook：%s" % ("移除" if remove else "写入", path))
    if DRY:
        return
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".ai-collab-tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(str(tmp), str(path))


def set_block(path, block_text, remove=False):
    old = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    if BEGIN in old and END in old:
        head, rest = old.split(BEGIN, 1)
        _, tail = rest.split(END, 1)
        old = head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")
    old = old.rstrip("\n")
    new = old if remove else (old + "\n\n" if old else "") + BEGIN + "\n" + block_text.strip() + "\n" + END
    say("%s协作约定：%s" % ("移除" if remove else "写入", path))
    if DRY:
        return
    backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new.rstrip("\n") + "\n", encoding="utf-8", newline="\n")


# 本工具以前版本（或打包前手动安装）的 SKILL.md 指纹：遇到这些可以放心升级
LEGACY_SKILLS = {
    "resume-other-agent": {"98fa6958de34e80870b6a91bca3db37a31ad1439e7f2bab11083a838060be358"},
}


def skill_hash(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def same_skill(src, target):
    """目标目录里的 SKILL.md 与本工具当前或以前的版本一致（例如以前手动装的同一份），可以接管。"""
    a, b = src / "SKILL.md", target / "SKILL.md"
    if not (a.exists() and b.exists()):
        return False
    hb = skill_hash(b)
    return hb == skill_hash(a) or hb in LEGACY_SKILLS.get(src.name, set())


def copy_skill(name, target_root, remove=False):
    src = KIT / "skills" / name
    target = target_root / name
    ours = (target / SKILL_MARK).exists()
    if remove:
        if target.exists() and ours:
            say("移除 skill：%s" % target)
            if not DRY:
                shutil.rmtree(str(target))
        return
    if target.exists() and not ours and not same_skill(src, target):
        print("跳过 skill %s：%s 已存在且不是本工具安装的，不覆盖。" % (name, target))
        return
    say("安装 skill：%s" % target)
    if DRY:
        return
    if target.exists():
        shutil.rmtree(str(target))
    shutil.copytree(str(src), str(target))
    (target / SKILL_MARK).write_text("installed by ai-collab-kit\n", encoding="utf-8")


# ---------- Turntrail（接力：读取对方的原生会话记录生成交接材料） ----------

def turntrail_ok():
    code, out = run("turntrail --help", timeout=60)
    return code == 0 and "Turntrail" in out


def install_turntrail():
    if turntrail_ok():
        say("Turntrail 已可用（turntrail 命令存在），跳过安装。")
        return True
    for tool in ("git", "node", "npm"):
        if run("%s --version" % tool, timeout=60)[0] != 0:
            print("警告：找不到 %s，无法安装 Turntrail。接力功能（resume-other-agent）需要它；"
                  "装好 Node.js 20+ 和 Git 后重新运行 install.py 即可。" % tool)
            return False
    if TURNTRAIL_DIR.exists():
        say("使用已存在的 %s" % TURNTRAIL_DIR)
    else:
        say("克隆 Turntrail %s → %s" % (TURNTRAIL_TAG, TURNTRAIL_DIR))
        if not DRY:
            code, out = run('git clone --depth 1 --branch %s %s "%s"' % (TURNTRAIL_TAG, TURNTRAIL_REPO, TURNTRAIL_DIR))
            if code != 0:
                print("警告：克隆 Turntrail 失败：\n%s" % out)
                return False
    say("npm install + npm link（注册全局 turntrail 命令）")
    if DRY:
        return True
    for cmd, cwd in (("npm install --no-audit --no-fund --ignore-scripts", TURNTRAIL_DIR),
                     ("npm link", TURNTRAIL_DIR / "packages" / "cli")):
        code, out = run(cmd, cwd=cwd)
        if code != 0:
            print("警告：`%s` 失败：\n%s" % (cmd, out[-2000:]))
            return False
    if not turntrail_ok():
        print("警告：Turntrail 已安装，但当前终端还找不到 turntrail 命令。重新打开终端后再试 `turntrail --help`；"
              "仍不行请把 `npm prefix -g` 显示的目录加入 PATH。")
        return False
    say("Turntrail 安装完成。")
    return True


def set_global_ignore(remove=False):
    """让 git 在所有项目里忽略 .turntrail/（Turntrail 的本地会话记录，不该被提交）。"""
    code, current = run("git config --global --get core.excludesfile", timeout=30)
    current = current.strip() if code == 0 else ""
    ours = GLOBAL_IGNORE.as_posix()
    if remove:
        if current and Path(os.path.expanduser(current)).resolve() == GLOBAL_IGNORE.resolve():
            say("移除 git 全局忽略设置 core.excludesfile")
            if not DRY:
                run("git config --global --unset core.excludesfile", timeout=30)
        return
    target = Path(os.path.expanduser(current)) if current else GLOBAL_IGNORE
    text = target.read_text(encoding="utf-8") if target.exists() else ""
    if IGNORE_LINE not in [l.strip() for l in text.splitlines()]:
        say("在 %s 中加入 %s" % (target, IGNORE_LINE))
        if not DRY:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(str(target), "a", encoding="utf-8", newline="\n") as f:
                if text and not text.endswith("\n"):
                    f.write("\n")
                f.write(IGNORE_LINE + "\n")
    if not current:
        say("设置 git config --global core.excludesfile %s" % ours)
        if not DRY:
            run('git config --global core.excludesfile "%s"' % ours, timeout=30)


def main():
    global DRY
    ap = argparse.ArgumentParser(description="安装 / 卸载 ai-collab-kit（Claude × Codex 自动留痕、互审与接力）")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", choices=["claude", "codex"])
    ap.add_argument("--no-turntrail", action="store_true", help="不安装 Turntrail")
    args = ap.parse_args()
    DRY = args.dry_run
    rm = args.uninstall
    do_claude = args.only in (None, "claude")
    do_codex = args.only in (None, "codex")
    rules = (KIT / "templates" / "global_rules.md").read_text(encoding="utf-8")

    py = None if rm else pick_python()
    if not rm:
        say("使用的 python 命令：%s" % py)
        for name in HOOK_FILES:
            say("复制 %s → %s" % (name, DEST))
            if not DRY:
                DEST.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(KIT / "hooks" / name), str(DEST / name))

    if do_claude:
        set_hooks(CLAUDE_HOME / "settings.json", "claude", CLAUDE_HOOKS, py, remove=rm)
        set_block(CLAUDE_HOME / "CLAUDE.md", rules, remove=rm)
        copy_skill("ask-codex", CLAUDE_HOME / "skills", remove=rm)
        copy_skill("resume-other-agent", CLAUDE_HOME / "skills", remove=rm)
    if do_codex:
        set_hooks(CODEX_HOME / "hooks.json", "codex", CODEX_HOOKS, py, remove=rm)
        set_block(CODEX_HOME / "AGENTS.md", rules, remove=rm)
        copy_skill("ask-claude", AGENTS_SKILLS, remove=rm)
        copy_skill("resume-other-agent", CODEX_HOME / "skills", remove=rm)

    tt = None
    if rm:
        set_global_ignore(remove=True)
    elif not args.no_turntrail:
        set_global_ignore()
        tt = install_turntrail()

    if DRY:
        print("\n以上是预览，没有改动任何文件。去掉 --dry-run 才会真正执行。")
    elif rm:
        print("\n已卸载。日志、~/.ai-collab 目录和 Turntrail 保留着；"
              "不需要 Turntrail 的话运行 `npm rm -g turntrail` 并删除 %s。请重启 Claude 和 Codex。" % TURNTRAIL_DIR)
    else:
        print("\n安装完成。请完全退出并重新打开 Claude 和 Codex，然后在任意 git 项目里让它们改个文件，看项目根目录有没有出现 AI_LOG.md。")
        if tt is False:
            print("注意：Turntrail 没装好，接力功能暂不可用，见上面的警告。")
        print("随时可以运行 `python %s` 自检。" % (DEST / "doctor.py").as_posix())


if __name__ == "__main__":
    main()
