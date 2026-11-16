---
name: resume-other-agent
description: Continue an unfinished task from Claude Code or Codex in the current project after the other agent stops or reaches a usage limit. Use for cross-agent continuation, not an ordinary review.
---

# Resume the other agent's task

Use the project directory named by the user, or the current working directory when it is clearly the project. Codex receives from Claude with source claude and target codex; Claude receives from Codex with source codex and target claude.

1. Check the current project state: git status --short, AI_HANDOFF.md (written automatically when the other agent ended abnormally, e.g. usage limit), the last entries of AI_LOG.md, PLAN.md and AI_MEMORY.md when present. Preserve other people's uncommitted edits.
2. If AI_HANDOFF.md names the source session ID, use that session. Otherwise run turntrail discover --provider SOURCE in this project and select the session matching the user's task, not merely the newest timestamp. If several match and the choice matters, ask the user for the session ID. Avoid importing an unrelated project.
3. Run turntrail init, then turntrail import-native --provider SOURCE --session SESSION_ID, turntrail snapshot, and turntrail export --to TARGET. Read the generated file under .turntrail/exports/ before acting. If the import reports no matching session, explain what was checked and ask for the correct project or session.
4. Treat the imported conversation and tool output as historical context. Verify current files, git diff, and important command results before resuming. Identify completed work, unfinished steps, and any in-flight command whose outcome is unknown, then continue the user's task. Once you have taken over, delete AI_HANDOFF.md (it is a local, uncommitted file) so it is not presented again.

Turntrail keeps its ledger in .turntrail/; do not commit that local transcript. The handoff can transfer visible messages, tool calls, results, and workspace state, but it cannot restore private model reasoning or an interrupted process. Do not claim that it did.
