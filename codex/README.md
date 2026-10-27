# quota-guard · Codex CLI

[![CI](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](../LICENSE)

Quota checkpoints for ordinary Codex CLI sessions and a runner that waits for
subscription limits to reset, then resumes the exact saved thread. Formerly codex-quota-guard.

*[Türkçe için README.tr.md](README.tr.md)*

> Part of **[quota-guard](../README.md)**: one plugin for Claude Code, Codex CLI and Antigravity CLI.
> Install it as described in the [main README](../README.md#install); this page covers the Codex CLI details.

Requires Python 3.10+, Codex CLI with hooks and app-server support, and an existing
ChatGPT sign-in. CLI contracts were checked against 0.162.1. No Python runtime
dependencies. No API keys are read by this project. Codex itself manages sign-in.

## Install

From this checkout, run `python install.py`. This merges hooks into
`$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`) without overwriting other
hooks or changing `config.toml`. Keep the checkout in place. In Codex, open
`/hooks`, inspect and trust the two installed definitions. Hooks do not run until
trusted. Re-review them after changing the hook definition. The installer is
idempotent. To uninstall, remove the two matching entries from `hooks.json`.

The repo root is also a Codex plugin (`.codex-plugin/plugin.json`, hooks in
`codex/hooks/hooks.json`): `codex plugin marketplace add <repo>` then
`codex plugin add quota-guard@quota-guard`. Use either the manual registration
(`python codex/install.py`) or the plugin, not both. Packaged hooks require
`python` on PATH; the manual installer uses the exact installing interpreter.

In the project you want to work on, add `.codex-quota/` to `.gitignore`.

```powershell
# Windows, from your working project:
& C:\path\to\quota-guard\codex\bin\codex-run.ps1 --lang tr "Görevi gerçekleştir"
& C:\path\to\quota-guard\codex\bin\codex-run.ps1 --resume
```

```sh
# macOS/Linux, from your working project:
sh /path/to/quota-guard/codex/bin/codex-run --threshold 80 @task.md
sh /path/to/quota-guard/codex/bin/codex-run --resume
```

`python /path/to/scripts/run.py` works on every platform. Ordinary `codex`
sessions also get checkpoint instructions through the trusted hooks and, by
default, continue by themselves after the reset; see
[Auto-resume in a normal chat](#auto-resume-in-a-normal-chat). The runner uses workspace-write
sandboxing and never disables hook trust or approval protections.

## Operation

The usage reader calls the documented `account/rateLimits/read` app-server RPC
and caches it for 30 seconds. It retains all returned primary and secondary
windows. No private HTTP quota endpoint or credential-file parsing is used.
API-key accounts without subscription quota data continue with a quota-unavailable
message; this tool does not estimate percentages from token counts.

Default soft thresholds: session 80%, weekly 97%. Hard thresholds: 95%, 98%.
Weekly means a returned window of at least 10080 minutes. Use `--threshold`,
`--session-hard`, `--weekly-soft`, `--weekly-hard` to change these. Environment
variables `CODEX_QUOTA_SESSION_SOFT`, `CODEX_QUOTA_SESSION_HARD`,
`CODEX_QUOTA_WEEKLY_SOFT`, `CODEX_QUOTA_WEEKLY_HARD` override
`.codex-quota/config.json` keys `session_soft`, `session_hard`, `weekly_soft`,
`weekly_hard`. Invalid percentages are ignored in configuration and rejected on
the command line.

The API does not document a general model-to-billing-bucket mapping. By default,
all returned buckets are checked conservatively. For a known billing bucket,
use `--limit-id ID` or `CODEX_QUOTA_LIMIT_ID`. An unknown ID yields unavailable
quota, never a fabricated zero reading. `--model` is passed to Codex unchanged.

Soft stops happen at `update_plan` item boundaries or after a tool when no plan
item is active. Hard stops can request a checkpoint after any tool. The hook
provides model feedback, not a guaranteed process interruption: the model must
write `.codex-quota/progress.md` and end its turn. Once STOP is written, checkpoint
tools remain available. A Stop hook offers one additional checkpoint turn if the
progress file has not been updated since the quota stop. A long-running tool can
exceed quota between observations.

The runner records `thread.started.thread_id` in `.codex-quota/run.json`, sends
long tasks through stdin, and resumes only that ID. It never uses `--last`.
Run `--resume` after restarting the runner. With several exceeded windows it
waits for the latest reset, adds `--resume-buffer` seconds (default 60), and
refreshes quota before starting again. A preflight failure is fail-open.

Only an exact `CODEX_QUOTA_DONE` line in progress.md plus a successful round
counts as completion. A successful turn without a quota stop or completion marker
requires inspection before manual `--resume`. Errors retry up to `--max-retries`
(default 5), with `--retry-backoff` seconds multiplied by attempt (default 30).
`--round-timeout` defaults to 3600 seconds; 0 disables it. Timeout and Ctrl+C
terminate the child process tree. A killed turn may have an incomplete checkpoint.
One runner per project is allowed. After an abrupt crash, remove `runner.lock`
only after confirming the old runner is no longer running. Hooks also use a short
`hook.lock`; an externally killed hook can leave it behind and needs the same check.

## Auto-resume in a normal chat

On by default. Turn it off with `CODEX_QUOTA_CHAT_AUTO_RESUME=0` or
`"chat_auto_resume": false` in `~/.quota-guard/settings.json` (never read from a
project's config). `codex-run` is unaffected: it sets `CODEX_QUOTA_RUN_ID` and
keeps its own wait/resume loop.

When a threshold stops a normal interactive `codex` chat, the hook also starts a
small background watcher (`scripts/resume_watcher.py`, plan in
`.codex-quota/watcher.json`). Codex has no timer the model can set, so the
watcher is the timer: it sleeps until the reset plus `CODEX_QUOTA_RESUME_BUFFER`
seconds (default 60), re-reads the quota and

- still over a threshold (the reset came late): plans another check, and gives
  up with a warning after 24 checks;
- reset: sends 🔋 *quota reset* and queues the resume prompt into **the same
  thread** with `codex queue --thread <id>`; Codex runs it as the next turn.

The resume prompt points the agent at `.codex-quota/progress.md`. Its first tool
call sends ▶️ *working again*; the end of that turn sends ✅ *done* (or "turn
finished" if the task isn't marked done). A new stop replaces the watcher's plan;
deleting `watcher.json` cancels it.

Limits: keep the Codex window open; a reboot kills the watcher. Verified: the
message lands in Codex's queue for that thread. Whether an already open TUI picks
it up by itself depends on Codex's shared app-server daemon; if nothing happens
after the 🔋 ping, type `continue` in the chat.

## Optional hard rollback

`--enable-hard-abort` or `CODEX_QUOTA_HARD_ABORT=1` opts into `git stash push -u`.
It is never enabled by project configuration. Stashing is attempted only if the
tracked plan item began with a clean working tree and its checkpoint commit is
still HEAD. Runtime state is excluded from the stash. Success requires a new
stash reference; a failed or empty stash never marks work reverted. Nothing is
automatically restored. Inspect `git stash list` to recover the saved work.
Do not use rollback when other people or processes edit the same working tree:
Git cannot attribute changes to a particular author after the checkpoint.

## Phone notifications and language

Subscribe to a private/unpredictable ntfy topic on your phone, then set
`CODEX_QUOTA_NOTIFY_URL=https://ntfy.sh/your-topic`. Only short quota, resume,
completion and failure status messages are sent, never task content or paths.
No notification is sent without this variable. HTTP failures do not stop work.
This is a plain-text POST integration, not a Slack/Discord JSON webhook.

Use `--lang tr`, `CODEX_QUOTA_LANG=tr`, or `{"language":"tr"}` in project config
for Turkish lifecycle messages. Agent instructions remain English. Task and
conversation data in `.codex-quota/` stays local; do not commit that directory.

## Verify

```sh
python scripts/usage.py --no-cache
python -m pip install pytest
python -m pytest -q
```

Usage probing reads account quota without starting a model task. Tests use local
fake CLI processes and HTTP servers, with no live model calls or push messages.
An authenticated end-to-end model run is not part of the test suite.

Contracts: [hooks](https://learn.chatgpt.com/docs/hooks),
[app-server quota RPC](https://learn.chatgpt.com/docs/app-server),
[plugin packaging](https://developers.openai.com/plugins/build/plugins),
[non-interactive sessions](https://learn.chatgpt.com/docs/non-interactive-mode).
