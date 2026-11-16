#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai-collab 钩子：被 Claude / Codex 的 hook 调用，负责留痕、简报、防撞车和异常交接。

用法（由 install.py 写进两边的 hook 配置，不用手动运行）：
    --event start      你发消息时：拍“开工前”快照；新会话的第一条消息附上项目简报
                       （共享记忆、交接、最近日志）；另一方正在同一目录工作时附上提醒
    --event stop       AI 回答结束时：再拍一张，差异就是这一轮的改动，写进 AI_LOG.md
    --event fail       回合因 API 错误结束（Claude StopFailure，比如额度用完）：
                       写日志 + 生成 AI_HANDOFF.md 交接文件
    --event modelfail  Codex 某次模型请求失败（PostModelRequest status=failed）：
                       只更新 AI_HANDOFF.md（请求可能会重试，不写日志）

不依赖 AI 自觉，也不依赖某个工具的“编辑文件”事件，所以 Claude 和 Codex 通用。
- git 仓库里：记录写到项目根目录的 AI_LOG.md（init_project.py 会让 git 在本机忽略它）。
- 不是 git 的文件夹：文件数不多时也能统计，记录写到 ~/.ai-collab/log-global.md。
- 只用标准库。任何失败都静默退出（输出 {} 并返回 0），绝不阻塞或打断 AI 干活；
  出错原因写进 ~/.ai-collab/debug.log，方便排查。
- 写进日志、交接文件的提问和回答会先做密钥脱敏。

环境变量：
    AI_COLLAB_LOG_ALL=1    连没有改文件的轮次也记录
    AI_COLLAB_NO_PROMPT=1  日志里不记录你的提问原文（涉及保密内容时建议开）
    AI_COLLAB_NO_AWAKE=1   不在 AI 工作期间阻止 Windows 空闲睡眠（见 awake_hook.py）
    AI_COLLAB_NO_BRIEF=1   不在新会话开始时注入项目简报
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

BASE = Path.home() / ".ai-collab"
STATE_DIR = BASE / "state"
GLOBAL_LOG = BASE / "log-global.md"
DEBUG_LOG = BASE / "debug.log"
LOG_NAME = "AI_LOG.md"
HANDOFF_NAME = "AI_HANDOFF.md"
MEMORY_NAME = "AI_MEMORY.md"
PLAN_NAME = "PLAN.md"
NAMES = {"claude": "Claude", "codex": "Codex"}
MAX_FILES = 5000          # git 里“已修改/未跟踪”文件超过这个数，就不再逐个统计
DIR_MAX_FILES = 3000      # 非 git 文件夹：最多扫描这么多文件
DIR_MAX_SECONDS = 3.0     # 非 git 文件夹：最多扫描这么久
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode"}
LOCAL_ONLY = {LOG_NAME, HANDOFF_NAME}   # 本工具自己写的文件，不算 AI 的改动
LIVE_SECONDS = 10 * 60        # 另一方的会话记录文件这么久内有更新，就认为它正在工作
LIVE_FALLBACK = 30 * 60       # 拿不到会话记录文件时：一轮开始后这么久内没结束，就认为它可能正在工作
ORPHAN_SECONDS = 24 * 3600    # 超过这个时间的未结束轮次不再提起
HANDOFF_MAX_AGE = 7 * 86400   # 交接文件超过 7 天不再放进简报
BRIEF_LIMIT = 2200            # 简报总字数上限（Codex 默认约 2500 token）
HEADER = (
    "# AI 工作日志\n\n"
    "由 ai-collab 的 hook 自动追加，记录每一轮是谁（Claude / Codex）改了哪些文件。请勿手动编辑。\n"
)
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # Windows 下不要弹黑色命令行窗口


# ---------------------------------------------------------------- 工具函数

def debug(msg):
    try:
        BASE.mkdir(parents=True, exist_ok=True)
        with open(str(DEBUG_LOG), "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


# 常见密钥格式。宁可多遮一点，也不要把密钥写进日志。
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(sk|pk|rk)-(ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
]
SECRET_ASSIGN = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:api[_-]?key|token|secret|password|passwd|pwd|密码|密钥)[A-Za-z0-9_]*)"
    r"(\s*[:=：]\s*)([\"']?)([^\s\"',;]{6,})")


def redact(text):
    if not text:
        return text
    for pat in SECRET_PATTERNS:
        text = pat.sub("[已隐藏的密钥]", text)
    return SECRET_ASSIGN.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + "[已隐藏]", text)


def run_git(cwd, *args):
    try:
        r = subprocess.run(
            ["git", "-c", "core.quotepath=off"] + list(args),
            cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=8, creationflags=NO_WINDOW,
        )
    except Exception:
        return None
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def git_root(cwd):
    out = run_git(cwd, "rev-parse", "--show-toplevel")
    return Path(out.strip()) if out and out.strip() else None


def file_sig(path):
    try:
        st = path.stat()
        return "%d:%d" % (st.st_size, st.st_mtime_ns)
    except OSError:
        return "gone"


def snapshot_git(root):
    """git 仓库：{'head': 当前提交, 'files': {相对路径: 签名}}，只含“已修改/未跟踪(且未被忽略)”的文件。"""
    head = (run_git(root, "rev-parse", "HEAD") or "").strip()
    out = run_git(root, "ls-files", "-m", "-o", "--exclude-standard", "-z") or ""
    files = {}
    for i, rel in enumerate(p for p in out.split("\0") if p):
        if i >= MAX_FILES:
            files["__truncated__"] = "1"
            break
        files[rel] = file_sig(root / rel)
    return {"head": head, "files": files}


def snapshot_dir(folder):
    """非 git 文件夹：遍历统计。文件太多或太慢就放弃（返回 None），避免拖慢 AI。"""
    start, files = time.time(), {}
    for dirpath, dirnames, filenames in os.walk(str(folder)):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if len(files) >= DIR_MAX_FILES or time.time() - start > DIR_MAX_SECONDS:
                return None
            p = Path(dirpath) / name
            files[os.path.relpath(str(p), str(folder)).replace("\\", "/")] = file_sig(p)
    return {"head": "", "files": files}


def take_snapshot(root, base):
    return snapshot_git(root) if root else snapshot_dir(base)


def diff_snapshots(before, after, base):
    """返回 (这一轮亲手改动的文件, 这一轮提交的、但不是这一轮改的文件)。"""
    edited = set()
    for p in set(before["files"]) | set(after["files"]):
        if p == "__truncated__":
            continue
        b, a = before["files"].get(p), after["files"].get(p)
        if b is not None and a is None:
            # 开工前有改动、现在不在“待提交”列表里：要么被提交了（文件内容没变→不算这一轮改的），
            # 要么被还原/删除了（文件签名变了→算）
            if file_sig(base / p) != b:
                edited.add(p)
        elif b != a:
            edited.add(p)
    committed = set()
    if before["head"] and after["head"] and before["head"] != after["head"]:
        out = run_git(base, "diff", "--name-only", "-z", before["head"], after["head"]) or ""
        committed = set(p for p in out.split("\0") if p)
    edited -= LOCAL_ONLY
    committed -= LOCAL_ONLY
    return sorted(edited), sorted(committed - edited)


def one_line(text, limit):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def safe_key(session_id):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(session_id))[:64] or "nosession"


def fmt_files(files, n=8):
    shown = "、".join("`%s`" % f for f in files[:n])
    return shown + ("（共 %d 个）" % len(files) if len(files) > n else "")


def ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return "%d 分钟前" % max(1, seconds // 60)
    if seconds < 86400:
        return "%.1f 小时前" % (seconds / 3600.0)
    return "%d 天前" % (seconds // 86400)


def state_path(sid):
    return STATE_DIR / (safe_key(sid) + ".json")


def read_state(path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def write_state(sid, state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = state_path(sid).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    os.replace(str(tmp), str(state_path(sid)))


def cleanup_old_state():
    cutoff = time.time() - 14 * 86400
    try:
        for f in list(STATE_DIR.glob("*.json")) + list(STATE_DIR.glob("*.briefed")):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except OSError:
        pass


def other_sessions(sid, place):
    """同一目录里其他会话的状态（不含自己）。"""
    for f in STATE_DIR.glob("*.json"):
        if f.stem == safe_key(sid):
            continue
        s = read_state(f)
        if s and s.get("place") == place:
            yield s


def overlapping(sid, place, t0, t1):
    """本轮 [t0, t1] 期间，同一个目录里还有哪些别的会话在工作（用来提醒“改动归属可能混在一起”）。"""
    found = set()
    for s in other_sessions(sid, place):
        o0, o1 = s.get("t0", 0), s.get("t1")
        # 对方已结束：时间段有交集；对方还没结束：它在 30 分钟内开始（更早的多半是中断后没收尾的残留）
        if (o1 is not None and o0 < t1 and o1 > t0) or (o1 is None and o0 < t1 and t1 - o0 < 1800):
            found.add(NAMES.get(s.get("agent"), s.get("agent") or "另一个会话"))
    return sorted(found)


def read_text(path, limit=None):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""
    return text if limit is None else text[:limit]


def clip(text, limit, source):
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…（已截断，完整内容见 %s）" % source


def md_section(text, title_words):
    """取出 markdown 里标题含 title_words 的那一节（到下一个同级或更高级标题为止）。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(#+)\s*(.*)", line)
        if m and any(w in m.group(2) for w in title_words):
            level = len(m.group(1))
            body = []
            for nxt in lines[i + 1:]:
                m2 = re.match(r"^(#+)\s", nxt)
                if m2 and len(m2.group(1)) <= level:
                    break
                body.append(nxt)
            return "\n".join(body).strip()
    return ""


def strip_comments(text):
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def last_log_entries(log_text, n):
    parts = re.split(r"\n(?=### )", log_text)
    entries = [p.strip() for p in parts if p.strip().startswith("### ")]
    return entries[-n:]


# ---------------------------------------------------------------- 简报与提醒（start）

def unfinished_others(sid, place, now, snap, base):
    """其他会话中“开始了但还没结束”的轮次，附上它开始以来变化过的文件。"""
    out = []
    for s in other_sessions(sid, place):
        if s.get("t1") is not None:
            continue
        if not s.get("sid") or s.get("sid") == "nosession":
            continue  # 旧版本留下的、或工具没提供会话 ID 的记录，无法判断归属，不报
        age = now - s.get("t0", now)
        if age > ORPHAN_SECONDS:
            continue
        changed = []
        if s.get("snap") and snap:
            changed, _ = diff_snapshots(s["snap"], snap, base)
        out.append((age, s, changed, is_live(s, now)))
    return sorted(out, key=lambda x: x[0])


def is_live(s, now):
    """对方是否真的还在工作。会话记录文件（transcript）在 AI 工作时会不断追加，用它的修改时间当“心跳”；
    很多回合因中断或关窗口收不到 Stop，只看“开始了没结束”会误报。"""
    path = s.get("transcript")
    if path:
        try:
            return now - os.path.getmtime(path) < LIVE_SECONDS
        except OSError:
            pass
    return now - s.get("t0", 0) < LIVE_FALLBACK


def describe_unfinished(items):
    lines = []
    for age, s, changed, live in items:
        who = NAMES.get(s.get("agent"), s.get("agent") or "另一个会话")
        sid8 = safe_key(s.get("sid") or "")[:8]
        if live:
            head = "%s（会话 `%s`）正在这个目录里工作（这一轮开始于 %s，还没有结束）。" % (who, sid8, ago(age))
        else:
            head = "%s（会话 `%s`）%s开始的一轮没有正常结束（可能额度用完、被中断或崩溃）。" % (who, sid8, ago(age))
        lines.append("- " + head)
        if s.get("prompt"):
            lines.append("  它当时的任务：%s" % s["prompt"])
        if changed:
            lines.append("  从那时起变化过的文件：%s（改同一批文件前先和用户确认）" % fmt_files(changed))
    return "\n".join(lines)


def build_brief(base, root, unfinished):
    sections = []
    if unfinished:
        sections.append(("另一方未结束的任务", describe_unfinished(unfinished), 700))
    handoff = base / HANDOFF_NAME
    if handoff.exists() and time.time() - handoff.stat().st_mtime < HANDOFF_MAX_AGE:
        sections.append(("交接文件 %s（上一个 AI 异常结束时自动生成）" % HANDOFF_NAME,
                         read_text(handoff, 4000), 600))
    memory = strip_comments(read_text(base / MEMORY_NAME, 8000))
    if re.search(r"^\s*[-*\d]", memory, re.M):   # 有实际条目才放进来
        sections.append(("共享记忆 %s" % MEMORY_NAME, memory, 700))
    plan = read_text(base / PLAN_NAME, 20000)
    for words, title in ((("交接",), "PLAN.md 的交接"), (("当前进度",), "PLAN.md 的当前进度")):
        part = strip_comments(md_section(plan, words))
        if part:
            sections.append((title, part, 400))
    if root:
        entries = last_log_entries(read_text(root / LOG_NAME), 3)
        if entries:
            sections.append(("最近的工作日志（%s 最后 %d 条）" % (LOG_NAME, len(entries)), "\n".join(entries), 600))
    if not sections:
        return ""
    out, used = ["[ai-collab 项目简报] 目录：%s。以下内容由 hook 从项目文件中自动摘录。" % base], 0
    for title, body, limit in sections:
        limit = min(limit, BRIEF_LIMIT - used)
        if limit < 80:
            break
        body = clip(body, limit, title)
        used += len(body)
        out.append("## %s\n%s" % (title, body))
    return "\n\n".join(out)


def emit_context(text):
    if not text:
        sys.stdout.write("{}")
        return
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                        "additionalContext": text}}))
    # 用 ASCII 转义输出：Windows 下 stdout 默认是 GBK，直接输出中文会让 Claude/Codex 读到乱码


def on_start(agent, data, cwd, sid):
    cleanup_old_state()
    root = git_root(cwd)
    base = root or cwd
    place = str(base)
    snap = take_snapshot(root, base)
    now = time.time()
    raw_prompt = "" if os.environ.get("AI_COLLAB_NO_PROMPT") else redact(data.get("prompt") or "")
    state = {
        "agent": agent,
        "sid": str(sid),
        "t0": now,
        "place": place,
        "root": str(root) if root else None,
        "cwd": str(cwd),
        "snap": snap,
        "prompt": one_line(raw_prompt, 120),
        "prompt_long": one_line(raw_prompt, 600),
        "model": data.get("model"),
        "transcript": data.get("transcript_path"),
    }
    write_state(sid, state)
    if snap is None:
        debug("start(%s): %s 文件太多或扫描太慢，这个文件夹不做留痕（建议在项目里 git init）" % (agent, cwd))

    unfinished = unfinished_others(sid, place, now, snap, base)
    marker = STATE_DIR / (safe_key(sid) + ".briefed")
    if not marker.exists() and not os.environ.get("AI_COLLAB_NO_BRIEF"):
        marker.write_text(str(now), "utf-8")
        return build_brief(base, root, unfinished)
    active = [u for u in unfinished if u[3]]
    if active:
        return "[ai-collab 提醒]\n" + describe_unfinished(active)
    return ""


# ---------------------------------------------------------------- 结束（stop / fail）

def finish_turn(agent, data, sid, abnormal=None):
    """记录这一轮的改动。abnormal 为异常原因时，日志里标注并生成交接文件。返回 (state, edited)。"""
    state_file = state_path(sid)
    state = read_state(state_file) if state_file.exists() else None
    if state is None:
        debug("%s(%s): 没找到开工快照（session=%s）。说明 start 钩子没触发过——可能是刚装好还没重启，或该工具不支持 UserPromptSubmit 钩子。"
              % ("fail" if abnormal else "stop", agent, sid))
        return None, []
    state["t1"] = time.time()  # 记下结束时间，供别的会话判断“是否撞车”
    if abnormal:
        state["failed"] = abnormal
    try:
        write_state(sid, state)
    except OSError:
        pass
    before = state.get("snap")
    if not before:
        return state, []  # start 时就放弃了统计（文件太多），已记过 debug
    root = Path(state["root"]) if state.get("root") else None
    base = root or Path(state["cwd"])
    after = take_snapshot(root, base)
    if after is None:
        return state, []
    edited, committed_only = diff_snapshots(before, after, base)
    if not edited and not committed_only and not abnormal and not os.environ.get("AI_COLLAB_LOG_ALL"):
        return state, edited  # 这一轮没动任何文件（比如只是问答），不记，避免日志变成流水账

    who = NAMES.get(agent, agent)
    model = data.get("model") or state.get("model")
    if model:
        who += "（%s）" % model
    lines = ["", "### %s · %s · `%s`" % (time.strftime("%Y-%m-%d %H:%M"), who, safe_key(sid)[:8])]
    if root is None:
        lines.append("- 目录：`%s`（非 git 文件夹）" % base)
    if state.get("prompt"):
        lines.append("- 任务：%s" % state["prompt"])
    if abnormal:
        lines.append("- 结果：**异常结束**（%s），已生成交接文件 `%s`" % (abnormal, HANDOFF_NAME))
    else:
        result = one_line(redact(data.get("last_assistant_message") or ""), 240)
        lines.append("- 结果：%s" % (result or "（该工具没有提供回答摘要）"))
    lines.append("- 改动：%s" % (fmt_files(edited) if edited else "无"))
    if committed_only:
        lines.append("- 同时提交了本轮之前就存在的改动（可能是对方或你自己此前改的）：%s" % fmt_files(committed_only))
    others = overlapping(sid, state.get("place"), state.get("t0", 0), state["t1"])
    if others:
        lines.append("- 注意：本轮期间 %s 也在这个目录里工作，上面的改动可能混有对方的修改，归属不一定准确。" % "、".join(others))
    if "__truncated__" in before["files"] or "__truncated__" in after["files"]:
        lines.append("- 注意：未被 git 忽略的文件超过 %d 个，统计可能不完整（建议把数据集、素材目录加进 .gitignore）" % MAX_FILES)

    log = (root / LOG_NAME) if root else GLOBAL_LOG
    log.parent.mkdir(parents=True, exist_ok=True)
    new = not log.exists()
    with open(str(log), "a", encoding="utf-8", newline="\n") as f:
        if new:
            f.write(HEADER)
        f.write("\n".join(lines) + "\n")
    return state, edited


def write_handoff(agent, sid, state, reason, edited):
    """生成 AI_HANDOFF.md：让另一个 AI 知道上一轮停在哪、怎么接。"""
    root = Path(state["root"]) if state.get("root") else None
    base = root or Path(state["cwd"])
    who = NAMES.get(agent, agent)
    other = "Codex" if agent == "claude" else "Claude"
    lines = [
        "# 交接：%s 异常结束" % who, "",
        "由 ai-collab 自动生成，本机文件，不会提交。接手的 AI 读完、确认情况后可以删除它。", "",
        "- 时间：%s" % time.strftime("%Y-%m-%d %H:%M"),
        "- 原因：%s" % reason,
        "- 来源：%s，会话 ID `%s`%s" % (who, sid, "，模型 %s" % state["model"] if state.get("model") else ""),
        "- 这一轮开始于：%s" % time.strftime("%Y-%m-%d %H:%M", time.localtime(state.get("t0", time.time()))),
    ]
    if state.get("prompt_long"):
        lines.append("- 最后的任务：%s" % state["prompt_long"])
    lines.append("- 这一轮已改动的文件：%s" % (fmt_files(edited, 20) if edited else "无（或无法统计）"))
    if root:
        status = (run_git(root, "status", "--short") or "").strip().splitlines()
        log = (run_git(root, "log", "-3", "--oneline") or "").strip()
        lines += ["", "## git status --short", "```", "\n".join(status[:30]) or "（干净）"]
        if len(status) > 30:
            lines.append("…（共 %d 行）" % len(status))
        lines += ["```", "", "## 最近提交", "```", log or "（无）", "```"]
    lines += [
        "", "## 怎么接手",
        "在这个项目里打开 %s，说“接着做 %s 没做完的任务”。resume-other-agent 技能会用 Turntrail 导入上面的会话：" % (other, who),
        "```",
        "turntrail import-native --provider %s --session %s" % (agent, sid),
        "```",
        "同时看 PLAN.md 的“交接”和 AI_LOG.md 最后几条。被中断的命令结果未知，接手前请重新验证。",
    ]
    (base / HANDOFF_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def release_awake(agent, sid):
    try:
        import awake_hook
        awake_hook.stop(agent, sid)
    except Exception:
        debug("awake(%s/stop) failed: %s" % (agent, traceback.format_exc().strip().splitlines()[-1]))


def error_reason(data):
    for key in ("error_type", "error", "reason", "stop_reason", "status"):
        val = data.get(key)
        if isinstance(val, dict):
            val = val.get("type") or val.get("code") or val.get("message")
        if val:
            return one_line(redact(str(val)), 160)
    return "API 错误（工具没有提供具体原因）"


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="agent")
    ap.add_argument("--event", choices=["start", "stop", "fail", "modelfail"], required=True)
    args = ap.parse_args()
    out = ""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
        data = json.loads(raw) if raw.strip() else {}
        if args.event == "modelfail" and str(data.get("status", "")).lower() != "failed":
            sys.stdout.write("{}")   # 绝大多数模型请求都成功：尽快退出，不拖慢 Codex
            return 0
        cwd = Path(data.get("cwd") or os.getcwd())
        sid = data.get("session_id") or data.get("thread_id") or "nosession"
        if args.event == "start":
            try:
                import awake_hook
                awake_hook.start(args.agent, sid)
            except Exception:
                debug("awake(%s/start) failed: %s" % (args.agent, traceback.format_exc().strip().splitlines()[-1]))
            out = on_start(args.agent, data, cwd, sid)
        elif args.event == "stop":
            release_awake(args.agent, sid)
            finish_turn(args.agent, data, sid)
        elif args.event == "fail":
            release_awake(args.agent, sid)
            reason = error_reason(data)
            state, edited = finish_turn(args.agent, data, sid, abnormal=reason)
            if state:
                write_handoff(args.agent, sid, state, reason, edited)
        else:  # modelfail：请求可能会重试，只更新交接文件
            state = read_state(state_path(sid))
            if state and state.get("snap"):
                root = Path(state["root"]) if state.get("root") else None
                base = root or Path(state["cwd"])
                after = take_snapshot(root, base)
                edited = diff_snapshots(state["snap"], after, base)[0] if after else []
                write_handoff(args.agent, sid, state, "模型请求失败：" + error_reason(data), edited)
    except Exception:
        debug("%s/%s 出错：%s" % (args.agent, args.event, traceback.format_exc().strip().splitlines()[-1]))
        out = ""
    if args.event == "start":
        emit_context(out)
    else:
        sys.stdout.write("{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
