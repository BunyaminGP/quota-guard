# quota-guard · Antigravity CLI

[![CI](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](../LICENSE)

Quota-aware execution guard and auto-resume runner for **Google Antigravity CLI (`agy`)**.

*[Türkçe için README.tr.md](README.tr.md)*

> Part of **[quota-guard](../README.md)**: one plugin for Claude Code, Codex CLI and Antigravity CLI.
> Install it as described in the [main README](../README.md#install); this page covers the Antigravity CLI details.

quota-guard's Antigravity part monitors your 5-hour session and 7-day weekly quota buckets via `agy -p "/usage" --output-format json` without reading or modifying any authentication/credential files. When quota thresholds are reached, it coordinates a clean checkpoint into `.agy-quota/progress.md`, sleeps until the latest triggered quota window resets, and resumes the exact conversation by ID (`--conversation <ID>`).

## Features

- **Credential-free Quota Inspection (`scripts/usage.py`)**: Reads `.command.data.groups[].buckets[]` from `agy -p "/usage" --output-format json`, computes used percentage from `remaining_fraction`, preserves the full list of quota windows, and filters relevant buckets by active `modelName` / `--model` (`Gemini Models`, `Claude and GPT models`, `Gemini 4 Argon`).
- **Native Antigravity Lifecycle Hooks (`hooks.json`, `scripts/quota_gate.py`)**:
  - `PostToolUse`: Always outputs `{}` per the Antigravity hook contract while tracking `task_boundary` / `TodoWrite` / `Task*` / `update_plan` state in `.agy-quota/todos_state.json` and detecting `.agy-quota/progress.md` checkpoint writes.
  - `PreInvocation`: Injects soft/hard stop checkpoint instructions via `injectSteps` before the model runs so the model can save its checkpoint before termination.
  - `PreToolUse`: Denies non-checkpoint tool calls (`{"decision": "deny"}`) once a quota stop is active while explicitly allowing writes to `.agy-quota/progress.md`.
  - `PostInvocation`: Terminates the loop (`"terminationBehavior": "terminate"`) after the model has had the opportunity to write a checkpoint, logging an explicit missing-checkpoint warning if a hard stop forced termination before `.agy-quota/progress.md` was updated.
- **Safe Opt-In Hard Revert**: `hard_abort_enabled` is disabled by default and can only be enabled via environment variable (`AGY_HARD_ABORT=1`). Even when enabled, it only stashes if the task started on a clean working tree (`checkpoint_clean: true`) and verifies that `refs/stash` actually changed before marking an item `reverted` / `pending`.
- **Cross-Platform Python Runner (`scripts/agy_runner.py`, `bin/agy-run`, `bin/agy-run.cmd`)**:
  - Accepts arbitrary-length prompts via positional arguments or `stdin`.
  - Captures the exact conversation ID and resumes strictly with `agy --conversation <ID> -p` (never falls back to `-c` / `--continue`).
  - Terminates the entire CLI process tree on round timeout (`AGY_ROUND_TIMEOUT`).
  - Sleeps until the latest triggered `resets_at` + `AGY_RESUME_BUFFER` and stops when `^AGY_QUOTA_DONE` appears in `.agy-quota/progress.md`.

## Installation

Recommended: install it with the Antigravity plugin manager, then restart
Antigravity:
```bash
agy plugin install https://github.com/BunyaminGP/quota-guard
# or, from a quota-guard checkout:
agy plugin validate . --strict
agy plugin install .
```

The install scripts are the alternative that also puts the `agy-run` launcher on
`~/.local/bin` (use one of the two, not both):
```bash
./agy/install.sh        # Unix / macOS / Linux / Git Bash
```
```powershell
.\agy\install.ps1      # Windows PowerShell
```

`agy` 1.4.x note: `agy -p --mode X` takes `--mode` as its prompt, so `agy-run`
sends the prompt as one stream-json event instead
(`agy --mode X --input-format stream-json --output-format stream-json -p=`).

## Auto-resume in a normal chat

On by default. Turn it off with `AGY_CHAT_AUTO_RESUME=0` or
`"chat_auto_resume": false` in `~/.quota-guard/settings.json` (never read from a
project's config). `agy-run` is unaffected: it marks itself with
`AGY_QUOTA_RUNNER=1` and `.agy-quota/runner.json` and keeps its own loop.

When a threshold stops a normal `agy` chat, the stop instructions also tell the
agent to call Antigravity's own **`schedule`** tool once: a one-shot background
timer for the reset time (+ `AGY_RESUME_BUFFER` seconds, default 60; a far
weekly reset is reached in hops of at most 6 hours). That call is the one tool
still allowed while stopped. If the agent ends its turn without it, it gets one
nudge, then you get a ⚠️ ping.

When the timer fires, the same conversation wakes up. The hook sees that as the
`schedule` tool's `PostToolUse`, re-checks the quota and

- reset: sends 🔋 *quota reset*, and the agent continues from
  `.agy-quota/progress.md`; the first tool call sends ▶️ *working again*, the end
  of the turn (`Stop` hook) sends ✅ *done*;
- still over a threshold: stays stopped and asks the agent for another timer.

Keep the Antigravity window open until the reset.

## Usage

```bash
# Positional task
agy-run "Refactor the authentication module and run unit tests"

# Multi-line specification via stdin
agy-run < spec.md

# Specify model and permission mode
agy-run --model gemini-2.5-pro --mode accept-edits < spec.md
```

## Configuration

Project-level thresholds can be placed in `.agy-quota/config.json`:
```json
{
  "session_soft_limit": 80,
  "session_hard_limit": 95,
  "weekly_soft_limit": 97,
  "weekly_hard_limit": 98,
  "language": "en"
}
```
Security-sensitive settings (`AGY_HARD_ABORT=1`, `AGY_NOTIFY_URL=https://ntfy.sh/my-topic`) are read exclusively from environment variables, never from repository config files.
