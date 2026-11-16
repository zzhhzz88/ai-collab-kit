#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测试：在临时的假用户目录里安装 / 重复安装 / 留痕 / 卸载，不碰真实配置。

    python tests/test_kit.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parent.parent
PY = sys.executable
NL = chr(10)


class KitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex").mkdir(parents=True)
        self.env = dict(os.environ)
        for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
            self.env.pop(key, None)
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home),
                        GIT_CONFIG_GLOBAL=str(self.home / ".gitconfig"),
                        AI_COLLAB_NO_AWAKE="1", PYTHONIOENCODING="utf-8")
        # 用户原有的配置，安装/卸载都必须保留
        self.user_hook = {"hooks": [{"type": "command", "command": "echo mine"}]}
        (self.home / ".claude" / "settings.json").write_text(json.dumps(
            {"theme": "dark", "hooks": {"Stop": [self.user_hook]}}), encoding="utf-8")
        (self.home / ".codex" / "hooks.json").write_text(json.dumps(
            {"hooks": {"UserPromptSubmit": [self.user_hook]}}), encoding="utf-8")
        (self.home / ".claude" / "CLAUDE.md").write_text("我自己的规则\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_py(self, *args, stdin=None, cwd=None):
        r = subprocess.run([PY] + [str(a) for a in args], env=self.env, cwd=cwd,
                           input=(stdin or "").encode("utf-8"),
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        out = r.stdout.decode("utf-8", "replace")
        self.assertEqual(r.returncode, 0, out)
        return out

    def git(self, cwd, *args):
        subprocess.run(["git"] + list(args), cwd=str(cwd), env=self.env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def load(self, rel):
        return json.loads((self.home / rel).read_text(encoding="utf-8"))

    def count_ours(self, data):
        return sum(1 for groups in data.get("hooks", {}).values() for g in groups
                   for h in g.get("hooks", []) if ".ai-collab" in h.get("command", ""))

    def test_full_cycle(self):
        # 1. 安装两次（第二次应当幂等，不重复加 hook）
        self.run_py(KIT / "install.py", "--no-turntrail")
        self.run_py(KIT / "install.py", "--no-turntrail")
        claude = self.load(".claude/settings.json")
        codex = self.load(".codex/hooks.json")
        self.assertEqual(claude["theme"], "dark")
        self.assertEqual(self.count_ours(claude), 3)
        self.assertEqual(self.count_ours(codex), 4)
        self.assertIn(self.user_hook, claude["hooks"]["Stop"])
        self.assertIn(self.user_hook, codex["hooks"]["UserPromptSubmit"])
        self.assertIn("StopFailure", claude["hooks"])
        self.assertIn("Interrupt", codex["hooks"])
        self.assertIn("PostModelRequest", codex["hooks"])
        for rel in (".ai-collab/log_event.py", ".ai-collab/awake_hook.py",
                    ".claude/skills/ask-codex/SKILL.md", ".claude/skills/resume-other-agent/SKILL.md",
                    ".codex/skills/resume-other-agent/SKILL.md", ".agents/skills/ask-claude/SKILL.md"):
            self.assertTrue((self.home / rel).exists(), rel)
        rules = (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertIn("我自己的规则", rules)
        self.assertEqual(rules.count("<!-- ai-collab:begin -->"), 1)

        # 2. 项目初始化 + 模拟一轮 AI 改文件，检查 AI_LOG.md
        proj = Path(self.tmp.name) / "proj"
        proj.mkdir()
        self.run_py(KIT / "init_project.py", proj)
        for name in ("AGENTS.md", "CLAUDE.md", "PLAN.md", "AI_MEMORY.md"):
            self.assertTrue((proj / name).exists(), name)
        exclude = (proj / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn("AI_LOG.md", exclude)
        self.assertIn(".turntrail/", exclude)
        self.assertIn("AI_HANDOFF.md", exclude)

        hook = self.home / ".ai-collab" / "log_event.py"
        event = json.dumps({"session_id": "test-session", "cwd": str(proj), "prompt": "加一个 hello.py"})
        self.run_py(hook, "--agent", "claude", "--event", "start", stdin=event)
        (proj / "hello.py").write_text("print('hi')\n", encoding="utf-8")
        stop = json.dumps({"session_id": "test-session", "cwd": str(proj), "last_assistant_message": "已添加"})
        self.assertEqual(self.run_py(hook, "--agent", "claude", "--event", "stop", stdin=stop), "{}")
        log = (proj / "AI_LOG.md").read_text(encoding="utf-8")
        self.assertIn("Claude", log)
        self.assertIn("`hello.py`", log)
        self.assertIn("加一个 hello.py", log)

        # 只聊天、没改文件的轮次不记录（同一会话第二条消息，不再附简报）
        self.assertEqual(self.run_py(hook, "--agent", "claude", "--event", "start", stdin=event), "{}")
        self.run_py(hook, "--agent", "claude", "--event", "stop", stdin=stop)
        self.assertEqual((proj / "AI_LOG.md").read_text(encoding="utf-8"), log)

        # 3. 卸载：本工具的东西去掉，用户原有的保留
        self.run_py(KIT / "install.py", "--uninstall")
        claude = self.load(".claude/settings.json")
        codex = self.load(".codex/hooks.json")
        self.assertEqual(self.count_ours(claude), 0)
        self.assertEqual(self.count_ours(codex), 0)
        self.assertEqual(claude["hooks"], {"Stop": [self.user_hook]})
        self.assertEqual(codex["hooks"], {"UserPromptSubmit": [self.user_hook]})
        self.assertFalse((self.home / ".claude" / "skills" / "ask-codex").exists())
        self.assertNotIn("ai-collab", (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_existing_identical_skill_is_adopted_but_foreign_skill_kept(self):
        same = self.home / ".claude" / "skills" / "resume-other-agent"
        same.mkdir(parents=True)
        (same / "SKILL.md").write_bytes((KIT / "skills" / "resume-other-agent" / "SKILL.md").read_bytes())
        foreign = self.home / ".claude" / "skills" / "ask-codex"
        foreign.mkdir(parents=True)
        (foreign / "SKILL.md").write_text("别人的 skill\n", encoding="utf-8")
        self.run_py(KIT / "install.py", "--no-turntrail", "--only", "claude")
        self.assertTrue((same / ".ai-collab").exists())
        self.assertEqual((foreign / "SKILL.md").read_text(encoding="utf-8"), "别人的 skill\n")
        self.assertFalse((self.home / ".codex" / "skills").exists())

    def test_global_ignore(self):
        # 不加 --no-turntrail：会写 git 全局忽略（写进假的 GIT_CONFIG_GLOBAL），
        # Turntrail 若已在 PATH 上则跳过安装，否则会尝试安装——这里只检查忽略设置。
        if subprocess.run("turntrail --help", shell=True, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode != 0:
            self.skipTest("本机没有 turntrail 命令，跳过（避免测试时联网安装）")
        self.run_py(KIT / "install.py")
        ignore = (self.home / ".ai-collab" / "gitignore_global").read_text(encoding="utf-8")
        self.assertIn(".turntrail/", ignore)
        cfg = (self.home / ".gitconfig").read_text(encoding="utf-8")
        self.assertIn("excludesfile", cfg)
        self.run_py(KIT / "install.py", "--uninstall")
        self.assertNotIn("excludesfile", (self.home / ".gitconfig").read_text(encoding="utf-8"))

    # ---------------- 新功能：简报、撞车提醒、异常交接、脱敏、自检

    def setup_project(self):
        self.run_py(KIT / "install.py", "--no-turntrail")
        proj = Path(self.tmp.name) / "proj2"
        proj.mkdir()
        self.run_py(KIT / "init_project.py", proj)
        return proj, self.home / ".ai-collab" / "log_event.py"

    def hook(self, script, agent, event, payload):
        return self.run_py(script, "--agent", agent, "--event", event, stdin=json.dumps(payload))

    def context(self, out):
        if out == "{}":
            return ""
        return json.loads(out)["hookSpecificOutput"]["additionalContext"]

    def test_brief_on_first_prompt_only(self):
        proj, hook = self.setup_project()
        mem = proj / "AI_MEMORY.md"
        mem.write_text(mem.read_text(encoding="utf-8").replace("## 踩过的坑", "## 踩过的坑" + NL + "- 2026-11-17 [codex] 测试要加 -p no:cacheprovider"), encoding="utf-8")
        plan = proj / "PLAN.md"
        plan.write_text(plan.read_text(encoding="utf-8") + NL + "还差第 3 步：写文档" + NL, encoding="utf-8")
        # 先让 codex 留一条日志
        self.hook(hook, "codex", "start", {"session_id": "c1", "cwd": str(proj), "prompt": "写 a.py"})
        (proj / "a.py").write_text("x = 1" + NL, encoding="utf-8")
        self.hook(hook, "codex", "stop", {"session_id": "c1", "cwd": str(proj), "last_assistant_message": "写好了"})
        ctx = self.context(self.hook(hook, "claude", "start", {"session_id": "k1", "cwd": str(proj), "prompt": "继续"}))
        self.assertIn("项目简报", ctx)
        self.assertIn("no:cacheprovider", ctx)
        self.assertIn("还差第 3 步", ctx)
        self.assertIn("`a.py`", ctx)
        self.assertLessEqual(len(ctx), 2600)
        self.assertEqual(self.hook(hook, "claude", "start", {"session_id": "k1", "cwd": str(proj), "prompt": "再来"}), "{}")
        # 关闭简报（先结束 k1，否则 k2 会收到“k1 正在工作”的撞车提醒）
        self.hook(hook, "claude", "stop", {"session_id": "k1", "cwd": str(proj)})
        self.env["AI_COLLAB_NO_BRIEF"] = "1"
        self.assertEqual(self.hook(hook, "claude", "start", {"session_id": "k2", "cwd": str(proj), "prompt": "x"}), "{}")

    def test_empty_project_has_no_brief(self):
        proj, hook = self.setup_project()
        self.assertEqual(self.hook(hook, "claude", "start", {"session_id": "k1", "cwd": str(proj), "prompt": "hi"}), "{}")

    def test_lock_warning_while_other_agent_works(self):
        proj, hook = self.setup_project()
        self.env["AI_COLLAB_NO_BRIEF"] = "1"
        self.hook(hook, "codex", "start", {"session_id": "c1", "cwd": str(proj), "prompt": "重构 b.py"})
        (proj / "b.py").write_text("y = 2" + NL, encoding="utf-8")
        ctx = self.context(self.hook(hook, "claude", "start", {"session_id": "k1", "cwd": str(proj), "prompt": "改 b.py"}))
        self.assertIn("Codex", ctx)
        self.assertIn("重构 b.py", ctx)
        self.assertIn("`b.py`", ctx)
        # codex 结束后，提醒消失
        self.hook(hook, "codex", "stop", {"session_id": "c1", "cwd": str(proj)})
        self.assertEqual(self.hook(hook, "claude", "start", {"session_id": "k1", "cwd": str(proj), "prompt": "改 b.py"}), "{}")

    def test_stale_session_is_not_reported_as_working(self):
        proj, hook = self.setup_project()
        self.env["AI_COLLAB_NO_BRIEF"] = "1"
        transcript = Path(self.tmp.name) / "codex.jsonl"
        transcript.write_text("{}" + NL, encoding="utf-8")
        old = time.time() - 3600
        os.utime(str(transcript), (old, old))   # 会话记录一小时没更新：多半是中断了
        self.hook(hook, "codex", "start", {"session_id": "c1", "cwd": str(proj), "prompt": "x",
                                           "transcript_path": str(transcript)})
        self.assertEqual(self.hook(hook, "claude", "start", {"session_id": "k1", "cwd": str(proj), "prompt": "y"}), "{}")
        transcript.write_text("{}" + NL + "{}" + NL, encoding="utf-8")   # 对方又在写会话记录 → 正在工作
        self.assertIn("正在这个目录里工作", self.context(
            self.hook(hook, "claude", "start", {"session_id": "k1", "cwd": str(proj), "prompt": "y"})))

    def test_fail_writes_handoff_and_next_agent_sees_it(self):
        proj, hook = self.setup_project()
        self.hook(hook, "claude", "start", {"session_id": "sess-claude-1", "cwd": str(proj), "prompt": "实现登录功能"})
        (proj / "login.py").write_text("def login(): pass" + NL, encoding="utf-8")
        self.assertEqual(self.hook(hook, "claude", "fail", {"session_id": "sess-claude-1", "cwd": str(proj), "error": "rate_limit"}), "{}")
        handoff = (proj / "AI_HANDOFF.md").read_text(encoding="utf-8")
        for text in ("rate_limit", "sess-claude-1", "实现登录功能", "`login.py`", "turntrail import-native --provider claude --session sess-claude-1"):
            self.assertIn(text, handoff)
        log = (proj / "AI_LOG.md").read_text(encoding="utf-8")
        self.assertIn("异常结束", log)
        self.assertNotIn("AI_HANDOFF.md`、", log)  # 交接文件本身不算改动
        ctx = self.context(self.hook(hook, "codex", "start", {"session_id": "c9", "cwd": str(proj), "prompt": "接着做"}))
        self.assertIn("AI_HANDOFF.md", ctx)
        self.assertIn("rate_limit", ctx)

    def test_codex_modelfail(self):
        proj, hook = self.setup_project()
        self.hook(hook, "codex", "start", {"session_id": "c1", "cwd": str(proj), "prompt": "任务"})
        self.assertEqual(self.hook(hook, "codex", "modelfail", {"session_id": "c1", "cwd": str(proj), "status": "completed"}), "{}")
        self.assertFalse((proj / "AI_HANDOFF.md").exists())
        self.hook(hook, "codex", "modelfail", {"session_id": "c1", "cwd": str(proj), "status": "failed", "error": "usage_limit_reached"})
        self.assertIn("usage_limit_reached", (proj / "AI_HANDOFF.md").read_text(encoding="utf-8"))
        self.assertFalse((proj / "AI_LOG.md").exists())  # 请求可能重试，不写日志

    def test_redaction(self):
        proj, hook = self.setup_project()
        secrets = ["sk-abcdefghijklmnopqrstuvwxyz123456", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", "hunter2secret"]
        prompt = "用这个密钥 %s 和 %s，password=%s 部署" % tuple(secrets)
        self.hook(hook, "claude", "start", {"session_id": "k1", "cwd": str(proj), "prompt": prompt})
        (proj / "c.py").write_text("z = 3" + NL, encoding="utf-8")
        self.hook(hook, "claude", "stop", {"session_id": "k1", "cwd": str(proj),
                                           "last_assistant_message": "已用 Bearer abcdefghijklmnopqrstuvwxyz 部署"})
        log = (proj / "AI_LOG.md").read_text(encoding="utf-8")
        for secret in secrets + ["abcdefghijklmnopqrstuvwxyz"]:
            self.assertNotIn(secret, log)
        self.assertIn("已隐藏", log)
        self.assertIn("部署", log)

    def test_doctor(self):
        proj, _ = self.setup_project()
        r = subprocess.run([PY, str(self.home / ".ai-collab" / "doctor.py"), str(proj)], env=self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
        out = r.stdout.decode("utf-8", "replace")
        self.assertIn("留痕正常", out, out)
        self.assertIn("密钥脱敏正常", out, out)
        self.assertNotIn("缺少", out, out)
        self.assertFalse(list((self.home / ".ai-collab" / "state").glob("doctor-selftest-*")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
