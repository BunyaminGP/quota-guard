# quota-guard · Claude Code

[![CI](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml/badge.svg)](https://github.com/BunyaminGP/quota-guard/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](../LICENSE)
[![Changelog](https://img.shields.io/badge/changelog-md-lightgrey.svg)](CHANGELOG.md)

Run Claude Code **quota-aware**: stop it at a clean checkpoint before you hit
your usage limit, save progress to disk, and **automatically resume once the
quota resets** — instead of getting cut off mid-task.

*[Türkçe için README.tr.md](README.tr.md)*

> Part of **[quota-guard](../README.md)**: one plugin for Claude Code, Codex CLI and Antigravity CLI.
> Install it as described in the [main README](../README.md#install); this page covers the Claude Code details.

## The problem

Normally, the moment your Claude Code quota runs out, the session just stops
— wherever it happened to be, edits half-made, nothing recorded. This tool
stops *before* that, at a point you choose, and in a way you choose:

- **SOFT threshold** (default 80% session / 97% weekly): there's still
  headroom, so Claude finishes the current todo item, then wraps up cleanly
  (commit + write progress notes) before stopping. Session and weekly are
  independent tiers — either can fire on its own.
- **HARD threshold** (default 95% session / 98% weekly): can fire mid-item,
  when there may not be enough headroom left to finish safely. **Off by
  default.** If you opt in, Claude's in-progress changes on that item are
  reverted with `git stash`, the item goes back to `pending`, and it gets
  redone from scratch after the reset. In an 8-item todo list, if item 6 hits
  the hard threshold halfway through: item 6's partial work is stashed away,
  the reset happens, item 6 is redone from scratch, then 7 and 8 proceed
  normally.

## Install

**Recommended — as a Claude Code plugin:**

```
/plugin marketplace add BunyaminGP/quota-guard
/plugin install quota-guard@quota-guard
```

That's it — no copying files by hand, no editing `settings.json`. The hooks
register automatically and start protecting every session immediately.

**Note:** a plugin's `bin/` directory is only added to PATH for Claude's own
internal Bash tool calls — not for *your* terminal. Since `cc-run` is meant
to be run by you (it's what starts a headless, self-resuming Claude run),
you need one small extra step to call it as a bare `cc-run` command from
your own shell — see [Using cc-run from your own terminal](#using-cc-run-from-your-own-terminal)
below. The hooks themselves (the actual quota protection) don't need this —
they work the moment the plugin is installed, regardless.

Install at project scope instead of user scope if you want it shared with
your team via version control:

```
claude plugin install quota-guard@quota-guard --scope project
```

### Configuration screen

When you enable the plugin, Claude Code prompts you for the thresholds —
this is the actual "settings screen" for the tool, declared via the
plugin's `userConfig`:

- **Soft threshold — session (%)** — default 80
- **Hard threshold — session (%)** — default 95
- **Soft threshold — weekly (%)** — default 97
- **Hard threshold — weekly (%)** — default 98
- **Auto-resume chats after the quota resets** — default on. See
  [Auto-resume in a normal chat](#auto-resume-in-a-normal-chat-no-cc-run)
- **Enable auto-revert (git stash) on the hard threshold** — default off
- **Notification webhook URL (optional)** — default empty (disabled). See
  [Notifications](#notifications)

To change these later without reinstalling:

```
claude plugin install quota-guard@quota-guard --config session_soft=70 --config hard_abort_enabled=true
```

(repeat `--config key=value` for each field you want to change), or run
`/plugin` inside a session and use its configure action for the plugin. The
values are stored in your own `~/.claude/settings.json` under
`pluginConfigs`, never in the plugin's files.

**Fallback — manual install** (for Claude Code versions without the plugin
system):

```bash
git clone https://github.com/BunyaminGP/quota-guard
cd quota-guard/claude
bash install.sh
```

This copies the tool into `~/.claude/cc-quota-guard/` and merges the
`PostToolUse` hooks into `~/.claude/settings.json` (idempotent — safe to
re-run, e.g. to pick up an update).

## Using cc-run from your own terminal

One-time setup so `cc-run` works as a bare command in *your* shell (not
required for the hooks/protection — only for the `cc-run` wrapper itself):

**Windows / PowerShell:**

```powershell
Get-Content shell\cc-run.ps1 | Add-Content $PROFILE
. $PROFILE
```

(requires Git for Windows, for Git Bash — `cc-run` is a bash script and
PowerShell can't run it directly). This defines a `cc-run` function that
resolves the plugin's current install path each time, so it survives plugin
updates automatically.

**macOS / Linux:**

```bash
cat shell/cc-run.sh >> ~/.bashrc   # or ~/.zshrc
source ~/.bashrc
```

Same idea — a shell function that re-resolves the plugin's install path on
every call. If you used the manual `install.sh` fallback instead of the
plugin system, you already have a stable path at
`~/.claude/cc-quota-guard/bin/cc-run` — just make sure `~/.local/bin` (where
`install.sh` symlinks it) is on your `PATH` and skip this step.

## Usage

Fully automatic (stop + auto-resume at reset):

```bash
cc-run "refactor the auth service in 3 steps: ..."
cc-run --threshold 80 --session-hard 95 --weekly-soft 97 --weekly-hard 98 @task.md
```

- `--threshold N` — session **SOFT** threshold (default 80). Finishes the
  current item, then stops.
- `--session-hard N` — session **HARD** threshold (default 95). Can fire
  mid-item.
- `--weekly-soft N` — weekly **SOFT** threshold (default 97). Finishes the
  current item, then stops.
- `--weekly-hard N` — weekly **HARD** threshold (default 98). Can fire
  mid-item.
- `--enable-hard-abort` — opt in to auto-reverting the in-progress item
  (`git stash`) when a HARD threshold fires. **Off unless you pass this.**
  Without it, HARD thresholds just force a clean stop, same as SOFT — nothing
  is touched automatically. Read [Safety](#safety-read-this-before-enabling-hard-abort)
  below before turning this on.
- `--model NAME` — which model to run (`opus`, `sonnet`, `fable`, or a full
  model name). If you don't pass this, it's whatever your `claude` CLI's own
  default is — same as running `claude` with no `--model`.
- Task: plain text or `@file.md` (the file's contents become the task).

`cc-run` prints the *resolved* model name at the start of every round (e.g.
`🧠 model: claude-sonnet-5`) — aliases like `opus` or `sonnet` mean "latest,"
so this is the only place you can see which specific dated model actually
ran. It also prints which tools are being called and, at the end of each
round, a cost/model-usage summary. This comes from piping
`--output-format stream-json` through `scripts/stream_render.py` — same
underlying run, no extra cost, just parsed instead of shown as raw text.

## Auto-resume in a normal chat (no cc-run)

You don't need `cc-run` for this. When a threshold stops Claude in an
ordinary interactive chat, the chat continues by itself after the reset:

1. The hook stops Claude as before (progress saved to
   `.cc-quota/progress.md`) and also tells it to schedule its own wake-up
   with Claude Code's built-in **CronCreate** tool — a one-time timer for
   about a minute after the reset. The stop message says when:
   *"Auto-resume: 14:15 (keep the Claude Code window open)."*
2. When Claude ends that turn, a `Stop` hook checks that the wake-up really
   exists. If it doesn't, Claude is asked once more; if it's still missing,
   you get a ⚠️ ping telling you to type "continue" yourself.
3. At that time the timer sends the chat a resume prompt. A
   `UserPromptSubmit` hook re-reads the quota: if it has really reset you
   get **🔋 Quota reset** and Claude picks up from `progress.md`; if not
   (the reset came late), Claude quietly schedules another wake-up.
4. Claude's first tool call after that pings **▶️ Claude is working
   again**, and the end of that turn pings **✅ Done**.

Limits — this rides on CronCreate, so:

- the Claude Code window must stay open (and the computer awake) until
  then — the timer lives in that session only;
- it fires only while the chat is idle; if you're typing at that moment, it
  waits for you;
- it can't help when the limit hits *before* the hook gets a chance (e.g.
  one huge reply). You then get a ⛔ "stopped by the usage limit" ping (from
  the `StopFailure` hook) and type "continue" yourself after the reset.

On by default. Turn it off with the `chat_auto_resume` option or
`CC_CHAT_AUTO_RESUME=0`. `cc-run` runs are unaffected — it resumes on its
own, so the chat logic stays out of its way.

**Try it without spending quota.** The simulation makes the hook see a fake
quota for a few minutes:

```bash
python3 scripts/usage.py --simulate 96 --reset-in 3   # 96% now, resets in 3 min, then 3%
python3 scripts/usage.py                               # shows the SIMULATED reading
python3 scripts/usage.py --simulate-off                # stop early
```

Then give Claude a small multi-step task in a chat. The fake reading lives
in `~/.claude/.cc-quota-sim.json` (your home directory, never the project,
so a cloned repo can't fake your quota) and turns itself off 30 minutes
after the fake reset. It affects every Claude Code window running this
version of the hook while it's on.

Thresholds for interactive sessions can also come from environment
variables (see
[Setting an environment variable](#setting-an-environment-variable) below):

```bash
export CC_SESSION_SOFT=80 CC_SESSION_HARD=95 CC_WEEKLY_SOFT=97 CC_WEEKLY_HARD=98 CC_HARD_ABORT=1
```

```powershell
$env:CC_SESSION_SOFT = "80"; $env:CC_SESSION_HARD = "95"; $env:CC_WEEKLY_SOFT = "97"; $env:CC_WEEKLY_HARD = "98"; $env:CC_HARD_ABORT = "1"
```

## How it works

1. **Hook** (`scripts/quota_gate.py`) — registered on a single `PostToolUse`
   `*` matcher (every tool call); it distinguishes planning-tool calls
   internally rather than needing a second matcher registration:
   - On a `TodoWrite` call — or, on Claude Code versions that plan with
     `TaskCreate`/`TaskUpdate`/`TaskList` instead, one of those — records
     which item is `in_progress` and which git commit it started from, into
     `.cc-quota/todos_state.json`; checks the SOFT thresholds (session and
     weekly are independent tiers, either can fire on its own). Both tool
     families are recognized because they're not interchangeable: a plugin
     that only understood one of them would have its SOFT threshold (and
     hard-abort's in-progress tracking) silently never fire on whichever
     Claude Code version uses the other one.
   - On **every** call: checks the HARD thresholds. A single todo item's
     work (many Edit/Bash/Write calls) can run long after one planning-tool
     call — checking only there would notice a quota spike far too late.
     If your plan reports a Sonnet-specific 7-day figure separately from
     the all-models weekly total, that's checked too, against the same
     `weekly_soft`/`weekly_hard` thresholds — a heavy-Sonnet session could
     otherwise burn through that cap while the all-models total still
     looks comfortably low.
2. **State files**
   - `.cc-quota/progress.md` — what's done, what's next, and (if hard-abort
     fired) which item was reverted. Read on resume.
   - `.cc-quota/todos_state.json` — the hook's own snapshot of the todo list.
3. **Wrapper** (`bin/cc-run`) — starts Claude, reads the reset time from
   `.cc-quota/STOP.json` when a threshold stops it, sleeps until then, and
   resumes with `claude -c`. If a round instead fails outright or hangs past
   `CC_ROUND_TIMEOUT` (e.g. a network outage right when it tried to resume),
   it retries with backoff rather than giving up silently. Stops when
   `progress.md` contains `CC_QUOTA_DONE`.
4. **Chat auto-resume** — the same hook script, also registered on
   `UserPromptSubmit`, `Stop` and `StopFailure` (helpers in
   `scripts/chat_resume.py`, state in `.cc-quota/chat_resume.json`). See
   [Auto-resume in a normal chat](#auto-resume-in-a-normal-chat-no-cc-run).

Quota source: Anthropic's **undocumented** OAuth usage endpoint
(`/api/oauth/usage`) — gives session (5h) and weekly utilization percentages
plus reset times.

## Notifications

Optional: get a push notification at the moments that matter (a threshold
stopped Claude, `cc-run` resumed, the task finished, or `cc-run` gave up
after retries — and in a normal chat: quota reset, Claude working again,
done) instead of having to watch the terminal.

**Setup (ntfy.sh — the only backend actually tested):**

1. Install the [ntfy](https://ntfy.sh) app (iOS/Android), or use it in a
   browser at ntfy.sh.
2. Subscribe to a topic name of your choosing. The public server has **no
   authentication** — anyone who knows your topic name can read what's
   published to it — so pick something hard to guess, not a plain word.
   e.g. `ccqg-yourname-a1b2c3`, not `test`.
3. Set `CC_NOTIFY_URL` to that address — as an environment variable (see
   [Setting an environment variable](#setting-an-environment-variable)), or
   in the plugin's configure screen (`notify_url`):

   ```bash
   export CC_NOTIFY_URL=https://ntfy.sh/your-topic-name
   ```

   ```powershell
   $env:CC_NOTIFY_URL = "https://ntfy.sh/your-topic-name"
   ```

That's it. The next threshold hit, resume, completion, or give-up pushes a
notification.

**What gets sent:** the exact same short status line already shown to you
(the hook's `systemMessage`, or `cc-run`'s own terminal line) — e.g.
*"Quota threshold reached (5-hour session 86%) — Claude is wrapping up
cleanly. Reset: ..."*. Never task content, file paths, or todo item text.

**Fail-open, like everything else in this tool:** no `CC_NOTIFY_URL` set,
a wrong URL, no network, or a timeout — nothing breaks. The notification
is silently skipped; the hook and `cc-run` continue exactly as if
notifications didn't exist. `CC_NOTIFY_TIMEOUT` (default 5s) caps how long
a single notification attempt can take.

**Any endpoint that accepts a raw-text HTTP POST body works the same
way** — but only ntfy.sh has actually been tested. This does **not** speak
Slack's or Discord's JSON webhook shape (`{"text": ...}` /
`{"content": ...}`); pointing `CC_NOTIFY_URL` directly at one of those
likely won't render correctly, since a plain-text body gets POSTed, not
JSON. Untested claim removed rather than left implied — see
[CHANGELOG.md](CHANGELOG.md).

**Security note:** like `hard_abort_enabled`, `notify_url`/`CC_NOTIFY_URL`
is deliberately **never** read from `.cc-quota/config.json` — only the env
var or the plugin's own configure screen can set it. A notify URL is where
your session status gets sent; letting a cloned/untrusted project's
`config.json` set it would let that project silently redirect status about
your session to a server you don't control.

## Safety — read this before enabling hard-abort

`--enable-hard-abort` makes an automated hook run `git stash` on your
working tree without asking. That's a reasonable thing to opt into once you
understand it; it is **not** a reasonable default for a plugin a stranger
just installed. A few things to know before you turn it on:

- **It relies on "clean at item start."** `git stash` returns the working
  tree to the last commit. For that to be the *right* commit, every item
  must actually be committed before the next one starts (the tool already
  pushes you toward this). If the previous item wasn't committed, the stash
  sweeps that up too — not malicious, just a known limitation.
- **It's recoverable, not automatic-recoverable.** `git stash` doesn't
  delete anything — see `git stash list` / `git stash pop` — but nothing
  restores it for you. That's intentional: an automatic un-revert would be
  one more thing that could silently do the wrong thing.
- **No git, no revert.** If the project isn't a git repository, hard-abort
  silently falls back to a clean stop — same as if you hadn't enabled it.
- **Try it somewhere disposable first.** A scratch repo, before you trust it
  on real work.
- **A project can't silently turn this on for you.** `hard_abort_enabled`
  can only be set by something *you* control — the `CC_HARD_ABORT` env var,
  or the plugin's own configure screen. It is deliberately never read from
  `.cc-quota/config.json`, because that file lives inside the project and
  can arrive already committed in a repo you clone. Only the percentage
  thresholds are project-configurable, since those just change *when* you
  stop, never *whether* something destructive-ish happens.
- **A stale or planted `STOP.json` can't disable the guard forever.** The
  hook only treats an existing `STOP.json` as "already stopped, don't
  re-check" while its `resets_at` is still in the future. One that's
  expired, malformed, or simply shipped inside a cloned project is ignored
  and cleaned up instead of silently turning protection off.
- **A planted `todos_state.json` can't trigger a stash on its own say-so.**
  Before honoring an `in_progress` claim for a hard-abort revert, the hook
  checks whether `todos_state.json` is itself tracked by git. It's meant to
  be local runtime state (see the `.gitignore` note below); a *tracked* copy
  is a sign it shipped with the repo rather than being written by this hook
  moments ago, so its `in_progress` claim is ignored instead of trusted.
- **`.cc-quota` can't be a symlink (or otherwise not-a-directory) and get
  away with it.** A cloned repo could otherwise commit `.cc-quota` as a
  symlink to somewhere else on disk (materialized as a real symlink on
  platforms where git does that by default) and have every write this hook
  makes land there instead of inside the project. The hook checks this
  before every write (and refuses instead of following it) and before every
  read of its own state files.
- **Percentage thresholds are clamped.** `session_soft` / `session_hard` /
  `weekly_soft` / `weekly_hard`, from any source (plugin config, `.cc-quota/config.json`,
  `CC_*` env vars), are only accepted in `(0, 100]`. This stops a cloned
  repo's `config.json` from setting e.g. `session_hard: 99999` to
  effectively disable the guard, or `session_soft: 0` to block every single
  tool call — and stops a typoed env var (e.g. `CC_SESSION_SOFT=80%`) from
  crashing the hook instead of being ignored.
- **Add `.cc-quota/` to your own project's `.gitignore`.** It's local
  runtime state (todo snapshots, stop markers), not something to commit —
  keeping it untracked also avoids it getting swept into a `git stash` along
  with real changes, and keeps the protections above from ever needing to
  kick in in the first place.

## Requirements

- `bash`, `python3`
- Claude Code CLI (`claude`), Pro/Max **subscription** (OAuth login) — the
  usage endpoint this tool reads only exists for that billing mode
- A valid OAuth token — from `CLAUDE_CODE_OAUTH_TOKEN` (if set), or
  `~/.claude/.credentials.json` (created automatically when you log into
  Claude Code), or on macOS specifically, the system Keychain as a
  fallback when that file doesn't exist (see
  [Honest warnings](#honest-warnings) — the Keychain path is implemented
  from Claude Code's own documented behavior, not yet verified against a
  real Mac).
- git, only if you plan to use `--enable-hard-abort`

### This tool does nothing on pay-as-you-go (API key) billing

If you run Claude Code with `ANTHROPIC_API_KEY` set (or another API-key based
setup) instead of an OAuth subscription login, there's no "5-hour session %"
or "weekly %" to read at all — that concept is specific to the Pro/Max
subscription plans. The hook fails open in that case: it doesn't error and
it doesn't block anything, it just quietly never triggers. You get zero
protection, not a degraded version of it — this tool doesn't currently track
$-based spending against a budget, which is the closest equivalent for
pay-as-you-go usage. If that's your billing mode and you want protection
too, please open an issue — it would need a different mechanism (tracking
token cost against a budget you set, not the OAuth usage endpoint).

## Verify / debug

```bash
python3 scripts/usage.py          # percentages as read
python3 scripts/usage.py --probe  # RAW API response (field-name check)
```

(For a plugin install, replace `scripts/` with the plugin's install path —
run `python3 "$CLAUDE_PLUGIN_ROOT/scripts/usage.py"` from a hook, or find the
cache path with `claude plugin list`.)

If `--probe` doesn't show `.five_hour.utilization` / `.seven_day.utilization`,
Anthropic likely changed the schema — update the field names in
`_normalize()` in `scripts/usage.py`.

## Honest warnings

- **The API is unofficial.** `/api/oauth/usage` is undocumented; Anthropic
  can change or remove it without notice. The hook **fails open**: if it
  can't read usage, it never blocks Claude. If the API goes down, protection
  silently turns off.
- **Detection isn't instant.** The hook now fires on every tool call (not
  just `TodoWrite`), so the real detection lag is roughly
  `CC_USAGE_CACHE_TTL` (default 30s) — much tighter than "a whole todo item,"
  but a single very large tool call inside that window can still overshoot a
  threshold. Set thresholds a bit below 100%, not right at it.
- **The machine has to stay on.** The wrapper sleeps until the reset; if the
  computer sleeps or shuts down, it won't resume on its own. A *transient
  network outage while the machine stays awake* is different and is
  handled: if a round (initial or resume) fails outright or makes zero
  progress for longer than `CC_ROUND_TIMEOUT` (default 1h — e.g. the network
  was down at the exact moment it woke up to resume), `cc-run` retries with
  backoff instead of silently giving up, up to `CC_MAX_RETRIES` times.
- **Don't rely on context recall.** `claude -c` brings the conversation back
  but isn't a hard guarantee. The real memory is `progress.md` + git commits
  — put anything critical there.
- **`acceptEdits` + git permission warning.** By default the wrapper runs
  with `--permission-mode acceptEdits --allowedTools "Bash(git add:*),Bash(git commit:*),Bash(git status:*)"` (so it
  can keep editing *and committing* headlessly across resumes without
  asking). The git allowance was added after a real headless run got
  permanently stuck: `acceptEdits` on its own auto-approves file edits but
  **not** Bash tool calls, and `git commit` specifically requires approval
  by default — with no one available to approve it in an unattended run,
  Claude gave up and ended the turn without ever committing, silently
  defeating this tool's whole progress-tracking model. Only `git add`,
  `git commit` and `git status` are granted, not `git *`: a blanket git grant
  would also let a headless run execute anything via `git -c ...`, aliases or
  `git diff --output=<file>` without asking. Other Bash commands
  a real task might need (running a test suite, package installs, etc.)
  are **not** covered by this default and can hit the same wall — if your
  task needs those unattended, add them yourself via `CC_CLAUDE_ARGS`
  (replaces the default entirely, so re-include
  `--permission-mode acceptEdits --allowedTools "Bash(git add:*),Bash(git commit:*),Bash(git status:*)" ...` plus
  whatever else you need). If none of this is acceptable, set
  `CC_CLAUDE_ARGS=""` and run interactively. Check `claude --help` for
  current flags — they can change.
- **Plan.** The usage endpoint works on Pro/Max. On a different plan,
  `--probe` may return nothing or something different.
- **macOS Keychain fallback is implemented but not yet verified on real
  hardware.** `scripts/usage.py` checks `CLAUDE_CODE_OAUTH_TOKEN`, then
  `~/.claude/.credentials.json`, then — on macOS, if that file doesn't
  exist — shells out to `security find-generic-password -s "Claude
  Code-credentials" -w` to read the token from the system Keychain. The
  service name and overall behavior are confirmed from Claude Code's own
  docs and public bug reports, not from running this against a real Mac
  (this project doesn't have one). If it doesn't work for your install,
  `python3 scripts/usage.py --probe` will show where it fails — please
  open an issue with that output.

## Settings

### Setting an environment variable

Every `CC_*` variable below is set the same way — syntax differs by shell.
Using `CC_SESSION_SOFT=80` as a stand-in for any of them:

**bash / zsh (Linux, macOS, Git Bash):**
```bash
export CC_SESSION_SOFT=80          # persists for the rest of this shell session
CC_SESSION_SOFT=80 cc-run "..."    # or inline, for one command only
```

**PowerShell (Windows):**
```powershell
$env:CC_SESSION_SOFT = "80"
cc-run "..."
```
This lasts only for the current PowerShell window. To make it permanent,
add the `$env:` line to your `$PROFILE` (the same file used in
[Using cc-run from your own terminal](#using-cc-run-from-your-own-terminal)
above).

There are three places to set the thresholds, checked in this order —
**each one overrides the ones below it**:

1. **`CC_*` environment variables** — an explicit, one-off override (what
   `cc-run` flags set when you actually pass them, or a manual `export`)
2. **`.cc-quota/config.json`** (per-project) — keys `session_soft`,
   `session_hard`, `weekly_soft`, `weekly_hard`, `language` (**not**
   `hard_abort_enabled` — see
   [Safety](#safety--read-this-before-enabling-hard-abort) for why that
   one is deliberately excluded from this file)
3. **The plugin's configuration screen** (see above) — your personal
   defaults, used everywhere you don't set 1 or 2

If none of the three are set anywhere, the built-in fallback is
80 / 95 / 97 / 98 / hard-abort off.

`hard_abort_enabled` and `notify_url` are the two exceptions to this
three-tier system — both are only ever readable from **1** or **3** above,
never from `.cc-quota/config.json`, since both are security-relevant (a
cloned/untrusted project's config.json must not be able to silently turn
on automatic `git stash`, or silently redirect your session status to a
URL you don't control). See [Safety](#safety--read-this-before-enabling-hard-abort)
and [Notifications](#notifications).

| Variable | Default | Description |
|---|---|---|
| `CC_SESSION_SOFT` | 80 | Session SOFT threshold (%) — finishes the item, then stops |
| `CC_SESSION_HARD` | 95 | Session HARD threshold (%) — can fire mid-item |
| `CC_WEEKLY_SOFT` | 97 | Weekly SOFT threshold (%) — finishes the item, then stops |
| `CC_WEEKLY_HARD` | 98 | Weekly HARD threshold (%) — can fire mid-item |
| `CC_HARD_ABORT` | (unset/false) | Opt in to auto-revert (`git stash`) on a HARD threshold. Same as `cc-run --enable-hard-abort` |
| `CC_NOTIFY_URL` | (unset) | Optional webhook URL for push notifications at key moments — tested against ntfy.sh. Never read from `.cc-quota/config.json`. See [Notifications](#notifications) |
| `CC_NOTIFY_TIMEOUT` | 5 | Max seconds a single notification attempt can take before giving up (fail-open either way) |
| `CC_USAGE_CACHE_TTL` | 30 | Usage cache lifetime (s) — also the hard-threshold detection lag |
| `CC_RESUME_BUFFER` | 60 | Extra sleep after the reset time (s) |
| `CC_CLAUDE_ARGS` | `--permission-mode acceptEdits` | Extra flags passed to `claude` |
| `CC_ROUND_TIMEOUT` | 3600 | Max seconds a single round (initial or resume) can run with zero progress before it's treated as stuck (e.g. the network dropped) and ended so it can be retried. `0` disables the cap. |
| `CC_MAX_RETRIES` | 5 | How many times in a row a round can end abnormally (non-zero exit, including a `CC_ROUND_TIMEOUT` cutoff) before `cc-run` gives up instead of retrying |
| `CC_RETRY_BACKOFF` | 30 | Base seconds between retries; grows linearly with each consecutive failure (attempt N waits `N × CC_RETRY_BACKOFF`s) |
| `CC_LANG` | `en` | `en` or `tr` — language for `cc-run`'s own terminal messages and the hook's user-facing status line. See [Language](#language--dil) below. |

## Language / Dil

Two things can be localized, independently of each other:

- **`cc-run`'s own terminal output** (start/resume/sleep/retry/done lines).
  Precedence: `CC_LANG` env var > `.cc-quota/config.json`'s `"language"` key
  > `en`. `cc-run` is invoked directly by you rather than by Claude Code's
  hook system, so it never sees the plugin's configure-screen setting
  (that env var is only populated for hook invocations) — set `CC_LANG`
  yourself, or add `"language": "tr"` to `.cc-quota/config.json`.
- **The hook's `systemMessage`** (the human-readable line Claude Code shows
  you when a threshold stops it). Precedence: `CC_LANG` > `.cc-quota/config.json`
  > the plugin's configure screen (`--config language=tr`) > `en`.

**Claude itself is always instructed in English, regardless of this
setting.** Only the text a *human* reads (`cc-run`'s terminal lines, the
hook's `systemMessage`) is ever translated — the `reason` field that tells
Claude what to do next is a model instruction, not something you read
directly, and this project's Claude-facing instructions are only tested in
English.

### Adding a language

Every user-facing string lives in one place: `locales/<code>.json` (one
file per language, e.g. `locales/en.json`, `locales/tr.json`). Both
`cc-run` and the hook read the same files through `scripts/i18n.py`, so
there's exactly one catalog to translate, not two.

**To add a language, copy `locales/en.json` to `locales/<code>.json` and
translate the values — that's it, no code changes anywhere.** The language
becomes selectable immediately (`CC_LANG=<code>`, `.cc-quota/config.json`'s
`"language"` key, or the plugin's configure screen). A few notes:

- Keep every `{placeholder}` exactly as-is; only translate the surrounding
  text. Placeholders are Python `str.format()` fields, so word order is
  completely free — put `{item}` wherever the sentence needs it.
- A partial translation is fine. Any key your file doesn't have (or a code
  nobody's added a file for at all) falls back to English automatically —
  see `i18n.msg()`'s fallback chain. Nothing crashes over a missing key.
- `bin/cc-run` has four bootstrap error messages (no task given, task file
  not found, `claude`/`python3` missing) that exist *before* python3's
  presence is confirmed, so they can't shell out to `i18n.py` — see `err()`
  near the top of that file. They're hardcoded English/Turkish only; adding
  a `locales/` file doesn't extend those four specifically (an accepted,
  narrow gap given how rare those invocation errors are).

## Uninstall

Plugin install:

```
/plugin uninstall quota-guard@quota-guard
```

Manual install:

```bash
rm -rf ~/.claude/cc-quota-guard ~/.claude/.cc-quota-cache.json ~/.local/bin/cc-run
# then remove the quota_gate.py entries (PostToolUse, UserPromptSubmit, Stop, StopFailure) from settings.json by hand
```

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](../CONTRIBUTING.md) for setup,
running the test suite, and the design principles a PR touching the
quota-detection or revert logic is expected to follow ("fail open, opt in
to anything destructive, document the limitation"). See
[CHANGELOG.md](CHANGELOG.md) for what's already shipped.

## License

MIT — see [LICENSE](../LICENSE).
