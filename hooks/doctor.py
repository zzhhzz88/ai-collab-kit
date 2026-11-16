#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai-collab 自检：检查安装是否完整、hook 是否生效、最近有没有报错，并做一次真实的留痕自测。

    python ~/.ai-collab/doctor.py               检查安装 + 当前文件夹
    python ~/.ai-collab/doctor.py D:\\code\\proj  同时检查这个项目
    python ~/.ai-collab/doctor.py --no-selftest  跳过自测

只读检查（自测会在临时文件夹里跑一次 hook，结束后清理），不会修改你的配置。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path.home()
BASE = HOME / ".ai-collab"
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (HOME / ".claude"))
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or (HOME / ".codex"))
KIT = Path(__file__).resolve().parent.parent if (Path(__file__).resolve().parent.parent / "install.py").exists() else None
EXPECT = {
    "claude": (CLAUDE_HOME / "settings.json",
               [("UserPromptSubmit", "log_event.py", "start"), ("Stop", "log_event.py", "stop"),
                ("StopFailure", "log_event.py", "fail")]),
    "codex": (CODEX_HOME / "hooks.json",
              [("UserPromptSubmit", "log_event.py", "start"), ("Stop", "log_event.py", "stop"),
               ("Interrupt", "awake_hook.py", "stop"), ("PostModelRequest", "log_event.py", "modelfail")]),
}
SKILLS = [CLAUDE_HOME / "skills" / "ask-codex", CLAUDE_HOME / "skills" / "resume-other-agent",
          HOME / ".agents" / "skills" / "ask-claude", CODEX_HOME / "skills" / "resume-other-agent"]
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

counts = {"ok": 0, "warn": 0, "bad": 0}


def report(level, msg, fix=None):
    counts[level] += 1
    print("  %s %s" % ({"ok": "[OK]", "warn": "[!!]", "bad": "[XX]"}[level], msg))
    if fix:
        print("       -> %s" % fix)


def run(cmd, cwd=None, stdin=None, env=None, timeout=60):
    try:
        r = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str), input=stdin, env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                           creationflags=NO_WINDOW)
        return r.returncode, r.stdout.decode("utf-8", "replace").strip()
    except Exception as e:
        return 1, str(e)


def sha(path):
    try:
        return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return None


def check_scripts():
    print("\n[脚本]")
    for name in ("log_event.py", "awake_hook.py", "doctor.py"):
        path = BASE / name
        if not path.exists():
            report("bad", "缺少 %s" % path, "重新运行 install.py")
        elif KIT and sha(path) != sha(KIT / "hooks" / name):
            report("warn", "%s 与安装包里的版本不同" % path, "在安装包目录运行 python install.py 更新")
        else:
            report("ok", str(path))


def check_hooks():
    print("\n[Hook 配置]")
    for agent, (path, expected) in EXPECT.items():
        if not path.exists():
            report("warn", "%s 不存在（没装 %s 或没运行 install.py）" % (path, agent))
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except ValueError as e:
            report("bad", "%s 不是合法 JSON：%s" % (path, e), "修好它，否则 %s 的所有 hook 都不生效" % agent)
            continue
        hooks = data.get("hooks", {})
        for event, script, ev in expected:
            cmds = [h.get("command", "") for g in hooks.get(event, []) for h in g.get("hooks", [])]
            hit = [c for c in cmds if script in c and "--event %s" % ev in c]
            if not hit:
                report("bad", "%s 缺少 %s hook（%s --event %s）" % (agent, event, script, ev), "重新运行 install.py")
                continue
            m = re.search(r'"([^"]+\.py)"', hit[0])
            if m and not Path(m.group(1)).exists():
                report("bad", "%s 的 %s hook 指向不存在的文件 %s" % (agent, event, m.group(1)), "重新运行 install.py")
            else:
                report("ok", "%s %s" % (agent, event))
            if len(hit) > 1:
                report("warn", "%s 的 %s hook 重复了 %d 次" % (agent, event, len(hit)), "重新运行 install.py 会去重")
    cfg = CODEX_HOME / "config.toml"
    if cfg.exists() and re.search(r"^\s*(codex_)?hooks\s*=\s*false", cfg.read_text(encoding="utf-8", errors="replace"), re.M):
        report("bad", "Codex 的 config.toml 关闭了 hooks", "删掉 [features] 里的 hooks = false")


def check_rules_and_skills():
    print("\n[协作约定与技能]")
    for path in (CLAUDE_HOME / "CLAUDE.md", CODEX_HOME / "AGENTS.md"):
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if "<!-- ai-collab:begin -->" in text:
            report("ok", "协作约定：%s" % path)
        else:
            report("warn", "%s 里没有协作约定" % path, "重新运行 install.py")
        if KIT and "AI_MEMORY.md" not in text and "<!-- ai-collab:begin -->" in text:
            report("warn", "%s 的协作约定是旧版本" % path, "重新运行 install.py")
    for path in SKILLS:
        report("ok" if (path / "SKILL.md").exists() else "warn", "技能 %s" % path,
               None if (path / "SKILL.md").exists() else "重新运行 install.py")


def check_turntrail():
    print("\n[Turntrail 接力]")
    code, out = run("turntrail --help")
    if code == 0 and "Turntrail" in out:
        report("ok", "turntrail 命令可用")
    else:
        report("bad", "找不到 turntrail 命令，接力功能不可用", "装 Node.js 20+ 后重新运行 install.py")
    code, path = run("git config --global --get core.excludesfile")
    target = Path(os.path.expanduser(path.strip())) if code == 0 and path.strip() else None
    if target and target.exists() and ".turntrail/" in target.read_text(encoding="utf-8", errors="replace"):
        report("ok", "git 全局忽略 .turntrail/")
    else:
        report("warn", "git 没有全局忽略 .turntrail/（会话记录可能被误提交）", "重新运行 install.py")


def tail_recent(path, days=7, n=3):
    if not path.exists():
        return []
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))
    lines = [l for l in path.read_text(encoding="utf-8", errors="replace").splitlines() if l[:10] >= cutoff]
    return lines


def check_logs():
    print("\n[最近 7 天的错误]")
    recent = tail_recent(BASE / "debug.log")
    errors = [l for l in recent if "出错" in l or "failed" in l]
    notes = [l for l in recent if l not in errors]
    if errors:
        report("warn", "debug.log 里有 %d 条错误，最近几条：" % len(errors))
        for l in errors[-3:]:
            print("         " + l[:200])
    else:
        report("ok", "debug.log 没有错误")
    if notes:
        report("warn", "有 %d 条提示（多为“文件太多，不做留痕”），最近一条：%s" % (len(notes), notes[-1][:160]))
    awake_errors = tail_recent(BASE / "awake" / "errors.log")
    if awake_errors:
        report("warn", "防睡眠有 %d 条错误，最近：%s" % (len(awake_errors), awake_errors[-1][:160]))
    leases = list((BASE / "awake").glob("*.json")) if (BASE / "awake").exists() else []
    stale = [p for p in leases if time.time() - p.stat().st_mtime > 6 * 3600]
    if stale:
        report("warn", "有 %d 个超过 6 小时的防睡眠记录（多半是异常结束的残留，4 小时兜底后已失效）" % len(stale))
    else:
        report("ok", "防睡眠：当前 %d 个进行中的回合" % len(leases))


def check_project(folder):
    print("\n[项目] %s" % folder)
    code, root = run(["git", "rev-parse", "--show-toplevel"], cwd=str(folder))
    if code != 0:
        report("warn", "不是 git 仓库：日志会写到 ~/.ai-collab/log-global.md，文件多时不留痕", "运行 init_project.py")
        return
    root = Path(root.strip())
    for name, hint in (("AGENTS.md", "运行 init_project.py"), ("PLAN.md", "运行 init_project.py"),
                       ("AI_MEMORY.md", "运行 init_project.py（已存在的文件不会被覆盖）")):
        report("ok" if (root / name).exists() else "warn", name, None if (root / name).exists() else hint)
    log = root / "AI_LOG.md"
    if log.exists():
        heads = re.findall(r"^### (\d{4}-\d\d-\d\d \d\d:\d\d) · ([^·]+)", log.read_text(encoding="utf-8", errors="replace"), re.M)
        report("ok", "AI_LOG.md 最后一条：%s" % (" ".join(heads[-1]).strip() if heads else "（空）"))
    else:
        report("warn", "还没有 AI_LOG.md（AI 还没在这里改过文件，或 hook 没生效）")
    if (root / "AI_HANDOFF.md").exists():
        report("warn", "有未处理的交接文件 AI_HANDOFF.md", "让另一个 AI“接着做没做完的任务”")
    mem = root / "AI_MEMORY.md"
    if mem.exists() and len(mem.read_text(encoding="utf-8", errors="replace")) > 6000:
        report("warn", "AI_MEMORY.md 超过 6000 字，简报只会带上前面一部分", "删掉过时条目")
    for name, probe in (("AI_LOG.md", "AI_LOG.md"), ("AI_HANDOFF.md", "AI_HANDOFF.md"), (".turntrail/", ".turntrail/x")):
        code, _ = run(["git", "check-ignore", "-q", "--no-index", probe], cwd=str(root))
        if code != 0:
            report("warn", "%s 没有被 git 忽略" % name, "运行 init_project.py")


def selftest():
    print("\n[自测：在临时 git 仓库里跑一轮 hook]")
    script = BASE / "log_event.py"
    if not script.exists():
        report("bad", "没装 log_event.py，跳过")
        return
    tmp = Path(tempfile.mkdtemp(prefix="ai-collab-doctor-"))
    sid = "doctor-selftest-%d" % os.getpid()
    env = dict(os.environ, AI_COLLAB_NO_AWAKE="1", PYTHONIOENCODING="utf-8")
    try:
        run(["git", "init", "-q"], cwd=str(tmp))
        start = json.dumps({"session_id": sid, "cwd": str(tmp), "prompt": "自测 token=abcdef123456"}).encode("utf-8")
        t = time.time()
        code, out = run([sys.executable, str(script), "--agent", "claude", "--event", "start"], stdin=start, env=env)
        (tmp / "selftest.txt").write_text("ok\n", encoding="utf-8")
        stop = json.dumps({"session_id": sid, "cwd": str(tmp), "last_assistant_message": "完成"}).encode("utf-8")
        code2, out2 = run([sys.executable, str(script), "--agent", "claude", "--event", "stop"], stdin=stop, env=env)
        cost = time.time() - t
        log = (tmp / "AI_LOG.md").read_text(encoding="utf-8") if (tmp / "AI_LOG.md").exists() else ""
        if code == 0 and code2 == 0 and "selftest.txt" in log:
            report("ok", "留痕正常（两次 hook 共 %.1f 秒）" % cost)
        else:
            report("bad", "留痕失败：start=%s %s / stop=%s %s" % (code, out[:100], code2, out2[:100]),
                   "看 ~/.ai-collab/debug.log")
        if "abcdef123456" in log:
            report("bad", "密钥脱敏没生效")
        elif log:
            report("ok", "密钥脱敏正常")
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
        for suffix in (".json", ".briefed"):
            try:
                (BASE / "state" / (sid + suffix)).unlink()
            except OSError:
                pass


def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="ai-collab 自检")
    ap.add_argument("project", nargs="?", default=".")
    ap.add_argument("--no-selftest", action="store_true")
    args = ap.parse_args()
    print("ai-collab 自检  (Python %s, %s)" % (sys.version.split()[0], sys.platform))
    check_scripts()
    check_hooks()
    check_rules_and_skills()
    check_turntrail()
    check_logs()
    check_project(Path(args.project).resolve())
    if not args.no_selftest:
        selftest()
    print("\n结果：%d 项正常，%d 项提醒，%d 项有问题。" % (counts["ok"], counts["warn"], counts["bad"]))
    if counts["bad"] == 0 and counts["warn"] == 0:
        print("一切正常。")
    return 1 if counts["bad"] else 0


if __name__ == "__main__":
    sys.exit(main())
