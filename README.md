# quota-guard

[![CI](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

One plugin for **Claude Code**, **OpenAI Codex CLI** and **Google Antigravity CLI
(`agy`)** that stops your coding agent at a clean checkpoint before it runs into
its subscription usage limit, and **continues the same chat by itself once the
quota resets**, with phone notifications along the way.

*[Türkçe: README.tr.md](README.tr.md)*

## What it does

- Watches your 5-hour session and weekly quota while the agent works.
- **Soft threshold** (default 80 % session / 97 % weekly): the agent finishes the
  current item, writes a checkpoint (`progress.md`) and stops cleanly.
  **Hard threshold** (95 % / 98 %): it stops right away.
- **In a normal chat, no wrapper command needed:** a wake-up is scheduled for the
  reset time, the quota is re-checked, and the same chat picks up from the
  checkpoint.
- **Notifications** to your phone (ntfy, or any URL that takes a plain-text POST):
  ⏸ stopped (with the resume time) → 🔋 quota reset → ▶️ working again → ✅ done,
  and ⚠️ when something needs you.
- Optional runners (`cc-run`, `codex-run`, `agy-run`) for unattended headless runs.

## How the chat wakes up

Each host has a different way to wake a chat up, so each one uses its own:

| Host | Wake-up | Hook events |
|---|---|---|
| Claude Code | the agent creates a one-shot `CronCreate` job for the reset time | PostToolUse, UserPromptSubmit, Stop, StopFailure |
| Codex CLI | a small background watcher; at the reset it runs `codex queue --thread <id>` | PostToolUse, Stop |
| Antigravity CLI | the agent sets a one-shot timer with Antigravity's `schedule` tool | PreInvocation, PreToolUse, PostToolUse, PostInvocation, Stop |

All three re-check the quota when they wake up. If the reset came late, they
wait again. The timing and state logic they share lives in `common/resume_core.py`.

## Install

Install it only for the tools you use: each recipe below stands alone. Each CLI
reads only its own manifest and loads only its own hooks. Requires Python 3.10+
on `PATH`.

**Claude Code**: inside Claude Code:

```
/plugin marketplace add BunyaminGP/quota-guard
/plugin install quota-guard@quota-guard
```

(or from a terminal: `claude plugin marketplace add BunyaminGP/quota-guard`, then
`claude plugin install quota-guard@quota-guard`). Restart Claude Code. Set the
thresholds, language, notification URL and auto-resume in `/plugin` →
quota-guard → *Configure*.

**Codex CLI**: from a terminal:

```bash
codex plugin marketplace add BunyaminGP/quota-guard
codex plugin add quota-guard@quota-guard
```

Then start Codex, open `/hooks` and **trust** the two quota-guard hooks. Codex
never runs untrusted hooks; check again after an update. Without the plugin
system: clone the repo and run `python quota-guard/codex/install.py`.

**Antigravity CLI**: `agy plugin install` takes a local folder, so clone first:

```bash
git clone https://github.com/BunyaminGP/quota-guard
agy plugin install ./quota-guard
```

Restart Antigravity. `quota-guard/agy/install.ps1` / `install.sh` are the
alternative that also installs the `agy-run` launcher (use one of the two).

**Notifications (optional, all tools):** subscribe to an ntfy topic on your phone
and put its URL in `~/.quota-guard/settings.json` (see the next section). Claude
Code can also take it from its *Configure* screen.

## Settings shared by all three

`~/.quota-guard/settings.json` (your home directory):

```json
{
  "notify_url": "https://ntfy.sh/<a-long-random-topic>",
  "language": "tr",
  "chat_auto_resume": true
}
```

Host-specific settings still win: the Claude plugin options / `CC_NOTIFY_URL`,
`CODEX_QUOTA_NOTIFY_URL`, `AGY_NOTIFY_URL`, and so on. The notification URL and
auto-resume switch are **never read from a project's files**, so a cloned repo
can't redirect your notifications. ntfy topics are public: anyone who guesses the
name can read them, so use a long random one.

## Limits

- The chat window has to stay open until the reset. A closed window, a sleeping
  or rebooted computer means no wake-up. The checkpoint is still there; just
  say "continue".
- Claude Code and Antigravity: the agent sets the timer itself. If it doesn't,
  it gets one reminder, then you get a ⚠️ notification.
- Codex: verified that the resume message lands in Codex's queue for that
  thread. Whether an already open TUI runs it by itself depends on Codex's
  app-server daemon. If nothing happens after the 🔋 notification, type
  `continue`.
- Subscription plans only (Claude Pro/Max, ChatGPT plans, Google AI plans). With
  API-key billing there is no quota to watch.
- Auto-revert with `git stash` on the hard threshold is off by default. Read the
  host README's safety section before turning it on.

## Repository layout

```
.claude-plugin/        Claude Code manifest + marketplace (hooks: claude/hooks/hooks.json)
.codex-plugin/         Codex manifest (hooks: codex/hooks/hooks.json)
.antigravity-plugin/   Antigravity manifest; its hooks are ./hooks.json (scripts in agy/)
common/                resume_core.py: shared resume timing, state and safe-write helpers
claude/ codex/ agy/    per-host hooks, runners, tests and docs
```

Host details: [Claude Code](claude/README.md) · [Codex CLI](codex/README.md) ·
[Antigravity CLI](agy/README.md)

## Tests

```bash
cd claude && python -m pytest -q && bash tests/test_cc_run.sh
cd codex  && python -m pytest -q
cd agy    && python -m pytest -q
```

The tests use fake CLIs and local HTTP servers: no model calls and no real
notifications. CI runs all three suites on Ubuntu, macOS and Windows.

## History

2.0.0 merges [cc-quota-guard](https://github.com/BunyaminGP/cc-quota-guard),
codex-quota-guard and agy-quota-guard into one repo and one plugin. See
[CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
