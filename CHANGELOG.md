# Changelog

All notable changes to quota-guard are documented here. Versions match
`.claude-plugin/plugin.json`, `.codex-plugin/plugin.json` and
`.antigravity-plugin/plugin.json`. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/).

Earlier history of the Claude Code part (cc-quota-guard ≤ 1.0.3):
[claude/CHANGELOG.md](claude/CHANGELOG.md).

## [2.0.0] — 2026-10-27

### Changed
- **One repo, one plugin.** cc-quota-guard, codex-quota-guard and
  agy-quota-guard are merged into `quota-guard`. Install it with
  `claude plugin install quota-guard@quota-guard`,
  `codex plugin add quota-guard@quota-guard` or `agy plugin install <repo>`.
  Each CLI reads only its own manifest and loads only its own hooks (checked
  with all three CLIs).

### Added
- **Chat auto-resume on all three hosts**, so you no longer need the runners
  to continue after a reset:
  - Claude Code: the agent schedules a one-shot `CronCreate` wake-up. New
    `UserPromptSubmit`, `Stop` and `StopFailure` (`rate_limit`) hooks.
  - Codex CLI: a detached watcher re-checks the quota at the reset and runs
    `codex queue --thread <id>` with the resume prompt.
  - Antigravity CLI: the agent sets a one-shot timer with Antigravity's
    `schedule` tool. The wake-up is that tool's `PostToolUse`, and a new `Stop`
    hook sends the "done" notification.
  - Notifications for every step: ⏸ stopped (with resume time), 🔋 quota
    reset, ▶️ working again, ✅ done, ⚠️ when you need to act.
  - Runners are unaffected: `CC_QUOTA_RUNNER`, `CODEX_QUOTA_RUN_ID`,
    `AGY_QUOTA_RUNNER` / `.agy-quota/runner.json`.
  - Antigravity verified live end to end (agy 1.4.8, simulated usage): stop →
    `schedule` timer → wake-up → 🔋 / ▶️ / ✅, task finished in the same
    conversation.
- `common/resume_core.py`: the resume timing (never on :00/:30, minute
  rounding, fallbacks), state, safe-write and detached-process helpers shared by
  all three hosts.
- `~/.quota-guard/settings.json`: per-user `notify_url`, `language` and
  `chat_auto_resume` for all three hosts (path overridable with
  `QUOTA_GUARD_CONFIG`). Host-specific settings still win, and it is never
  read from a project.
- Claude Code: `usage.py --simulate` for trying the stop → resume flow without
  spending quota.

### Fixed
- `agy-run` with agy 1.4.x: `agy -p --mode X` took `--mode` as the prompt. The
  prompt is now sent as one stream-json user event
  (`--input-format stream-json … -p=`).

### Security
- Claude Code and Antigravity state files (`STOP.json`, `todos_state.json`,
  `session.json`, `progress.md` notes, chat state) are written with a fresh
  `mkstemp` file + `os.replace`. A symlink a cloned repo planted at one of
  those paths is replaced, not followed (Codex already did this).
- `agy-run` refuses to start when `.agy-quota/` or `.agy-quota/progress.md` is
  a symlink, and writes `progress.md` atomically (it used to write through a
  planted link).
- `cc-run` grants only `git add`, `git commit` and `git status` by default
  instead of `Bash(git *)`, which also allowed `git -c …`, aliases and
  `git diff --output=<file>` without asking (command execution and file writes
  for a prompt-injected headless run).
- Codex: a `hook.lock` or `runner.lock` that is tracked by git (came with a
  cloned repo), stale, or names a dead process no longer switches the guard
  off.
