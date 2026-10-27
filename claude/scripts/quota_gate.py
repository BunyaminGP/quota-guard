#!/usr/bin/env python3
"""
quota_gate.py — Claude Code PostToolUse hook. Registered on a single "*"
matcher (every tool call); it distinguishes planning-tool calls internally
via `tool_name` rather than needing a second matcher registration:

  - On a TodoWrite call — OR, on Claude Code versions that plan this way
    instead, a TaskCreate/TaskUpdate/TaskList call — records the task list
    to `.cc-quota/todos_state.json` (which item is in_progress, and which
    git commit it started from) and checks the SOFT thresholds — session
    and weekly are independent tiers, either can fire on its own (finish
    the current item, then stop cleanly). Supporting both tool families
    matters because they're not interchangeable at the API level: TodoWrite
    sends the whole list, with statuses, in one call every time; TaskCreate/
    TaskUpdate are granular CRUD calls spread over many tool calls, with no
    single call carrying the full picture — see _save_task_tool_state().
    Recognizing only one of these two families means the SOFT thresholds and
    the in_progress bookkeeping (used for hard-abort revert) silently never
    fire on whichever family isn't recognized, no matter how high usage
    gets, since the specific call being watched for simply never happens.
  - On EVERY call: checks the HARD thresholds. A todo item's own work (many
    Edit/Bash/Write calls) can span a long stretch of time after a single
    planning-tool call — if we only checked there, a quota spike in
    between would be caught far too late.

What happens at the hard threshold depends on `hard_abort_enabled`
(default: **false**):
  - disabled (default): Claude is told to stop as safely as possible
    (commit if you can, don't start anything new) — same spirit as the
    soft stop, just triggered mid-item instead of at a boundary. No files
    are touched automatically.
  - enabled (opt-in via CC_HARD_ABORT=1, or the plugin's own userConfig
    screen): if a todo item is in_progress, ALL changes made on that item
    are reverted with `git stash`, the item is marked 'pending' again, and
    it gets redone from scratch after the quota resets.

Why is auto-revert opt-in? This hook is meant to be installed by people
who didn't write it. Automatically running `git stash` on someone's
working tree is a reasonable thing to opt into, not a reasonable default
for a stranger who just ran `/plugin install`. Read the README before
turning it on.

hard_abort_enabled can ONLY be turned on by something the user themselves
controls (an env var they set, or the plugin's configure screen) — never by
`.cc-quota/config.json`, because that file lives inside the project and can
arrive already committed in a repo someone else wrote or you just cloned.
The percentage thresholds ARE safe to read from config.json: they only
change *when* you stop, never *whether* a destructive-ish action happens.

FAIL-OPEN: if usage can't be read (API down, no token, schema changed),
Claude is NEVER blocked. Likewise, if git is unavailable or fails, revert
is skipped but Claude is still told to stop cleanly.

The hard-abort revert assumes the working tree is clean whenever a todo
item starts — i.e. every item ends with a commit. If the previous item
wasn't committed before the next one started, `git stash` will also sweep
up that earlier uncommitted work. This is a known limitation (see
README).

Config precedence for the percentage thresholds (highest wins): CC_* env
vars > .cc-quota/config.json > plugin userConfig > built-in defaults.
hard_abort_enabled uses the same order MINUS .cc-quota/config.json, which
is deliberately never consulted for it (see above). See _load_config().

Trust hardening (why these exist — all closing the same class of problem
as the STOP.json staleness check above: a cloned/untrusted repo can ship
files inside `.cc-quota/` even though that directory is meant to be local,
gitignored runtime state):
  - `.cc-quota` itself is never trusted if it's a symlink or an existing
    non-directory — writing through/over it could otherwise land anywhere
    on disk. See _quota_dir_is_safe().
  - A `todos_state.json` that is actually tracked by git (i.e. committed —
    the opposite of what the README tells users to do) is not trusted to
    claim an in_progress item for hard-abort revert purposes; a planted
    fake in-progress entry must not be able to trigger an automatic
    `git stash` on its own say-so. See _sanitize_state_for_revert().
  - Percentage thresholds from any source are clamped to (0, 100]; an
    out-of-range or unparsable value (e.g. a project shipping
    `session_hard: 99999` to effectively disable the guard, or a typoed
    CC_* env var) is ignored instead of applied or crashing the hook. See
    _parse_pct().
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import usage  # noqa: E402
except Exception:
    usage = None

try:
    import i18n  # noqa: E402
except Exception:
    i18n = None

try:
    import notify  # noqa: E402
except Exception:
    notify = None

try:
    import chat_resume  # noqa: E402
except Exception:
    chat_resume = None


# --------------------------------------------------------------------------- helpers

def _project_dir(hook_input):
    return (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or hook_input.get("cwd")
        or os.getcwd()
    )


def _quota_dir(proj):
    return os.path.join(proj, ".cc-quota")


def _quota_dir_is_safe(proj):
    """
    True only if `.cc-quota` doesn't exist yet, or already exists as an
    ordinary (non-symlinked) directory.

    The README tells users to .gitignore `.cc-quota/`, but nothing stops a
    cloned/untrusted repo from committing it anyway — including as a
    symlink. Git materializes a committed symlink as a real OS symlink on
    platforms where `core.symlinks` is on by default (Linux/macOS), so a
    repo could ship `.cc-quota -> /some/other/path` and have every write
    this hook makes (todos_state.json, STOP.json, progress.md, ...) land
    at that other path instead of inside the project. On platforms that
    don't materialize git symlinks (Windows by default), the same commit
    instead checks out as an ordinary file named `.cc-quota`, which would
    make `os.makedirs(..., exist_ok=True)` raise instead of silently
    writing elsewhere — still not something to let happen uncaught.

    Callers must treat False as "fail open": skip the write (or the read)
    rather than raising or following the symlink.
    """
    d = _quota_dir(proj)
    if os.path.islink(d):
        return False
    if os.path.exists(d) and not os.path.isdir(d):
        return False
    return True


def _marker_path(proj):
    return os.path.join(_quota_dir(proj), "STOP.json")


def _todos_state_path(proj):
    return os.path.join(_quota_dir(proj), "todos_state.json")


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _parse_pct(value):
    """
    Parses a percentage threshold, accepting only finite numbers in
    (0, 100]. Returns None — meaning "ignore this, keep whatever was set
    before" — for anything else: a value that doesn't parse as a number,
    NaN, zero, negative, or above 100.

    Applied uniformly to every source a threshold can come from (plugin
    userConfig, .cc-quota/config.json, CC_* env vars) so that:
      - a malformed value (e.g. a typoed CC_SESSION_SOFT="80%") never
        crashes the hook — previously only two of the three sources were
        wrapped in a try/except, so a bad env var took down every single
        tool-call hook invocation for the rest of the session;
      - an out-of-range value can't be used by a cloned/untrusted repo's
        `.cc-quota/config.json` to neuter the guard (e.g. session_hard
        set to 99999, which would never trigger) or to grief it into
        blocking every tool call (e.g. session_soft set to 0 or -1).
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v <= 0 or v > 100:  # v != v is the NaN check
        return None
    return v


def _parse_lang(value):
    """
    Returns a recognized language code, else None (ignore). "Recognized"
    means i18n.available_languages() finds a locales/<code>.json file for
    it — adding a new language is dropping that file in, nothing here
    needs to change. Falls back to accepting only "en" if the i18n module
    itself failed to import (matches its own FALLBACK_LANG).
    """
    v = str(value).strip().lower()
    if i18n is None:
        return v if v == "en" else None
    return v if i18n.is_known_language(v) else None


def _load_config(proj):
    """
    Precedence, lowest to highest (each step overrides the previous one):
      1. built-in fallback defaults
      2. plugin userConfig — the values the user picked in the install/
         configure screen (CLAUDE_PLUGIN_OPTION_* env vars, set by Claude
         Code itself when this hook runs as part of the plugin)
      3. .cc-quota/config.json — per-project override
      4. CC_* environment variables — an explicit, one-off override (e.g.
         cc-run flags the user actually passed, or a manual `export`)

    `language` follows the same four-tier precedence as the percentage
    thresholds — it's purely cosmetic (which language the human-facing
    systemMessage is written in), never security-relevant, so unlike
    hard_abort_enabled it's fine to read from any tier including
    config.json. Any code with a matching `locales/<code>.json` file is
    valid (see i18n.py) — adding a language is dropping in that one file,
    nothing here needs to change. Claude itself is always instructed in
    English (the `reason` field) regardless of this setting — see
    _label()/_soft_user_msg()/_hard_user_msg().
    """
    cfg = {
        "session_soft": 80.0,
        "session_hard": 95.0,
        "weekly_soft": 97.0,
        "weekly_hard": 98.0,
        "hard_abort_enabled": False,
        "language": "en",
        "chat_auto_resume": True,
    }

    plugin_option_map = {
        "CLAUDE_PLUGIN_OPTION_SESSION_SOFT": "session_soft",
        "CLAUDE_PLUGIN_OPTION_SESSION_HARD": "session_hard",
        "CLAUDE_PLUGIN_OPTION_WEEKLY_SOFT": "weekly_soft",
        "CLAUDE_PLUGIN_OPTION_WEEKLY_HARD": "weekly_hard",
    }
    for env, key in plugin_option_map.items():
        v = os.environ.get(env)
        if v not in (None, ""):
            parsed = _parse_pct(v)
            if parsed is not None:
                cfg[key] = parsed
    if os.environ.get("CLAUDE_PLUGIN_OPTION_HARD_ABORT_ENABLED") not in (None, ""):
        cfg["hard_abort_enabled"] = _truthy(os.environ["CLAUDE_PLUGIN_OPTION_HARD_ABORT_ENABLED"])
    if os.environ.get("CLAUDE_PLUGIN_OPTION_LANGUAGE") not in (None, ""):
        parsed_lang = _parse_lang(os.environ["CLAUDE_PLUGIN_OPTION_LANGUAGE"])
        if parsed_lang is not None:
            cfg["language"] = parsed_lang
    # chat_auto_resume, like hard_abort_enabled, is only read from places the
    # user controls (this screen, CC_CHAT_AUTO_RESUME) — never config.json.
    if os.environ.get("CLAUDE_PLUGIN_OPTION_CHAT_AUTO_RESUME") not in (None, ""):
        cfg["chat_auto_resume"] = _truthy(os.environ["CLAUDE_PLUGIN_OPTION_CHAT_AUTO_RESUME"])

    # NOTE: hard_abort_enabled is deliberately NOT read from config.json.
    # That file lives inside the project and can arrive already committed
    # in a repo you clone — it must never be able to silently turn on
    # automatic `git stash` on your behalf. Only a value YOU set yourself
    # (CC_HARD_ABORT env var, or the plugin's own userConfig screen) can do
    # that. Only the percentage thresholds are safe to let a project tune,
    # since they change *when* you stop, never *whether* something
    # destructive-ish happens.
    cfg_path = os.path.join(_quota_dir(proj), "config.json")
    try:
        with open(cfg_path) as f:
            fc = json.load(f)
        for k in ("session_soft", "session_hard", "weekly_soft", "weekly_hard"):
            if k in fc:
                parsed = _parse_pct(fc[k])
                if parsed is not None:
                    cfg[k] = parsed
        if "language" in fc:
            parsed_lang = _parse_lang(fc["language"])
            if parsed_lang is not None:
                cfg["language"] = parsed_lang
    except Exception:
        pass

    env_map = {
        "CC_SESSION_SOFT": "session_soft",
        "CC_SESSION_HARD": "session_hard",
        "CC_WEEKLY_SOFT": "weekly_soft",
        "CC_WEEKLY_HARD": "weekly_hard",
    }
    for env, key in env_map.items():
        if os.environ.get(env):
            parsed = _parse_pct(os.environ[env])
            if parsed is not None:
                cfg[key] = parsed
    if os.environ.get("CC_HARD_ABORT"):
        cfg["hard_abort_enabled"] = _truthy(os.environ["CC_HARD_ABORT"])
    if os.environ.get("CC_LANG"):
        parsed_lang = _parse_lang(os.environ["CC_LANG"])
        if parsed_lang is not None:
            cfg["language"] = parsed_lang
    if os.environ.get("CC_CHAT_AUTO_RESUME"):
        cfg["chat_auto_resume"] = _truthy(os.environ["CC_CHAT_AUTO_RESUME"])
    return cfg


def _allow():
    sys.exit(0)


def _block(reason, user_msg):
    # Reuses user_msg as-is: it's already the localized, human-facing
    # systemMessage (a status line — percentage, reset time — never task
    # content or file paths), so no new message needs to be drafted here.
    # Fires for both SOFT and HARD stops, since both funnel through this
    # one function. Best-effort: notify.notify() itself never raises, and
    # notify_url resolves to nothing unless the user configured it (see
    # notify.py's docstring for why it's never read from config.json).
    if notify is not None:
        notify.notify(user_msg, title="cc-quota-guard")
    out = {
        "decision": "block",
        "reason": reason,
        "systemMessage": user_msg,
        "continue": True,
    }
    print(json.dumps(out))
    sys.exit(0)


def _git(proj, *args):
    """
    Runs git, returning (code, stdout, stderr) — (1, "", <error>) on any
    failure to invoke it at all (fail-open: callers treat that the same as
    "git said no").

    Retries a couple of times first on OSError specifically: on some
    Windows/Python combinations, spawning many subprocesses in a short
    window can intermittently raise "OSError: [WinError 6] The handle is
    invalid" from stdio pipe setup — unrelated to git or the repo, and
    usually gone on the next attempt. Retrying here matters because a
    security-relevant caller (_is_git_tracked, used by
    _sanitize_state_for_revert) would otherwise read a transient OSError
    as "not tracked" and trust a claim it should have distrusted, purely
    from bad luck rather than the file genuinely being untracked.
    """
    last_err = "git invocation failed"
    for attempt in range(3):
        try:
            r = subprocess.run(
                ["git", "-C", proj, *args], capture_output=True, text=True, timeout=15
            )
            return r.returncode, r.stdout.strip(), r.stderr.strip()
        except OSError as e:
            last_err = str(e)
            time.sleep(0.05 * (attempt + 1))
        except Exception as e:
            return 1, "", str(e)
    return 1, "", last_err


def _is_git_repo(proj):
    code, out, _ = _git(proj, "rev-parse", "--is-inside-work-tree")
    return code == 0 and out == "true"


def _is_git_tracked(proj, relpath):
    """
    True if `relpath` (relative to `proj`, forward slashes) is committed/
    tracked by git. Used to distrust an in_progress claim in
    todos_state.json for hard-abort purposes: the README tells users to
    .gitignore `.cc-quota/` because it's local runtime state, so a tracked
    copy is a sign it shipped with the repo (possibly untrusted) rather
    than being written by this hook just now.
    """
    code, out, _err = _git(proj, "ls-files", "--error-unmatch", "--", relpath)
    return code == 0 and bool(out.strip())


def _read_todos_state(proj):
    if not _quota_dir_is_safe(proj):
        return None
    try:
        with open(_todos_state_path(proj)) as f:
            return json.load(f)
    except Exception:
        return None


def _write_json_atomic(path, data):
    """Every write into .cc-quota/ goes through here: a fresh, randomly named
    temp file (mkstemp creates it with O_EXCL, so it never opens anything that
    already exists), then os.replace(), which swaps `path` itself instead of
    following it. A fixed name like "STOP.json.tmp" would not be safe: a
    cloned repo could commit that name as a symlink to e.g. ~/.bashrc, and
    open(..., "w") would follow it and overwrite the target."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _write_todos_state(proj, state):
    if not _quota_dir_is_safe(proj):
        return
    os.makedirs(_quota_dir(proj), exist_ok=True)
    _write_json_atomic(_todos_state_path(proj), state)


def _new_in_progress(proj, content, is_git):
    checkpoint = None
    if is_git:
        code, out, _ = _git(proj, "rev-parse", "HEAD")
        checkpoint = out if code == 0 else None
    return {"content": content, "checkpoint_commit": checkpoint, "started_at": int(time.time())}


def _save_todos_state(proj, todos, is_git):
    """On a TodoWrite call, records the todo list; if a new item just became
    in_progress, notes the checkpoint commit (if any)."""
    prev = _read_todos_state(proj)
    prev_in_progress = (prev or {}).get("in_progress")
    in_progress_todo = next((t for t in todos if t.get("status") == "in_progress"), None)

    state = {"todos": todos, "updated_at": int(time.time())}
    if in_progress_todo is None:
        state["in_progress"] = None
    elif prev_in_progress and prev_in_progress.get("content") == in_progress_todo.get("content"):
        state["in_progress"] = prev_in_progress  # same item still running, keep checkpoint
    else:
        state["in_progress"] = _new_in_progress(proj, in_progress_todo.get("content"), is_git)
    _write_todos_state(proj, state)
    return state


def _save_task_tool_state(proj, tool_name, tool_input, tool_response, is_git):
    """
    Equivalent of _save_todos_state(), but for Claude Code's newer
    TaskCreate/TaskUpdate/TaskList tools rather than TodoWrite.

    Some Claude Code versions plan with this tool family INSTEAD of
    TodoWrite — if this hook only ever recognized TodoWrite, the SOFT
    threshold (gated on `tool_name == "TodoWrite"`) and the in_progress
    bookkeeping used for hard-abort revert would silently never fire on
    those versions, no matter how high usage got, since the one call it was
    watching for would simply never happen.

    Unlike TodoWrite (one call, the whole list, every time), these are
    granular CRUD calls spread over many tool calls, so state is built up
    incrementally here rather than replaced wholesale each time — except
    for TaskList, which does return the full list and is used as an
    opportunistic full resync when Claude happens to call it.
    """
    prev = _read_todos_state(proj) or {}
    todos = [dict(t) for t in (prev.get("todos") or [])]
    in_progress = prev.get("in_progress")

    def find(task_id):
        return next((t for t in todos if t.get("id") == task_id), None)

    if tool_name == "TaskCreate":
        task = (tool_response or {}).get("task") or {}
        task_id = task.get("id")
        # Falls back to a placeholder (matches the TaskUpdate branch below)
        # rather than storing None: content is later matched by equality
        # against aborted_item in _do_hard_stop's revert bookkeeping, and a
        # stored None would both defeat that match and (pre-fix) crash the
        # `aborted_item[:60]` slice there if this task became in_progress.
        subject = task.get("subject") or tool_input.get("subject") or f"task {task_id}"
        if task_id is not None and find(task_id) is None:
            todos.append({"id": task_id, "content": subject, "status": "pending"})

    elif tool_name == "TaskUpdate":
        task_id = (tool_response or {}).get("taskId") or tool_input.get("taskId")
        new_status = ((tool_response or {}).get("statusChange") or {}).get("to") or tool_input.get("status")
        entry = find(task_id) if task_id is not None else None
        if entry is None and task_id is not None:
            # A TaskUpdate for a task we never saw a TaskCreate for (e.g.
            # created before this hook started tracking it) — start
            # tracking it now rather than dropping the update.
            entry = {"id": task_id, "content": tool_input.get("subject") or f"task {task_id}", "status": "pending"}
            todos.append(entry)
        if entry is not None and new_status:
            entry["status"] = new_status
            if new_status == "in_progress":
                in_progress = _new_in_progress(proj, entry.get("content"), is_git)
            elif in_progress and in_progress.get("content") == entry.get("content"):
                in_progress = None  # the in-progress task finished/was deleted/etc.

    elif tool_name == "TaskList":
        tasks = (tool_response or {}).get("tasks")
        if tasks is not None:
            todos = [{"id": t.get("id"), "content": t.get("subject"), "status": t.get("status")} for t in tasks]
            still_running = next((t for t in todos if t.get("status") == "in_progress"), None)
            if still_running is None:
                in_progress = None
            elif not (in_progress and in_progress.get("content") == still_running.get("content")):
                in_progress = _new_in_progress(proj, still_running.get("content"), is_git)

    state = {"todos": todos, "in_progress": in_progress, "updated_at": int(time.time())}
    _write_todos_state(proj, state)
    return state


def _append_progress_note(proj, text):
    if not _quota_dir_is_safe(proj):
        return
    path = os.path.join(_quota_dir(proj), "progress.md")
    # Unlike the other writers below, this one is a plain append rather than
    # a write-to-temp-then-replace — and an append-mode open() ALWAYS
    # follows a symlink (replace() doesn't, which is why the temp+replace
    # writers are already safe). If progress.md itself (not just the
    # `.cc-quota` directory) were ever committed as a symlink, appending
    # here would write into whatever it points at. Refuse outright instead.
    if os.path.islink(path):
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + text + "\n")
    except Exception:
        pass


def _write_marker(proj, data):
    if not _quota_dir_is_safe(proj):
        return
    os.makedirs(_quota_dir(proj), exist_ok=True)
    # Never a direct open(STOP.json, "w"): that would follow a symlinked
    # STOP.json. See _write_json_atomic().
    _write_json_atomic(_marker_path(proj), data)


def _marker_still_valid(proj):
    """A STOP.json marker only short-circuits the checks below while we're
    still inside the quota window it declared (cc-run is asleep, waiting
    for `resets_at`). A marker whose `resets_at` has already passed — or
    that's missing/malformed — is stale: either cc-run resumed without
    cleaning it up, or the file shipped with the project (e.g. committed by
    someone else, or by an untrusted repo) rather than being written by
    this hook just now. A stale/foreign marker must NOT be trusted to
    silently and permanently disable the guard, so we treat it as absent.
    """
    try:
        with open(_marker_path(proj)) as f:
            data = json.load(f)
        resets_at = data.get("resets_at")
        if not resets_at:
            return False
        s = str(resets_at).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt > datetime.now(timezone.utc)
    except Exception:
        return False


# --------------------------------------------------------------------------- user-facing messages
#
# Only the systemMessage shown to the human is ever localized, via the
# shared i18n.py catalog (see its docstring for how to add a language).
# `reason` — the text that instructs Claude what to do — is always
# English, regardless of `language`: it's a model instruction, not
# something a person reads directly, and this codebase's Claude-facing
# instructions are only tested in English throughout.
#
# Each helper falls back to a hardcoded English string if the i18n module
# itself failed to import (fail-open: a broken/missing locales/ directory
# must never crash the hook or leave the human with no status line at all).

def _label(which, lang):
    key = "label_session" if which == "session" else "label_week"
    if i18n is None:
        base = "5-hour session" if which == "session" else "weekly"
    else:
        base = i18n.msg(lang, key)
    # "Sonnet" is a model name, not translatable prose — appended directly
    # rather than adding a locale key per language, so week_sonnet reuses
    # the exact same "weekly" translation as week_all instead of drifting.
    return (base + " (Sonnet)") if which == "week_sonnet" else base


def _soft_user_msg(lang, label, pct, resets_at):
    if i18n is None:
        return f"⛔ Quota threshold reached ({label} {pct:.0f}%) — Claude is wrapping up cleanly. Reset: {resets_at}"
    return i18n.msg(lang, "soft_stop", label=label, pct=f"{pct:.0f}", resets_at=resets_at)


def _hard_user_msg(lang, label, pct, resets_at, did_revert):
    if i18n is None:
        state = "item reverted" if did_revert else "clean stop"
        return f"⛔ HARD quota threshold ({label} {pct:.0f}%) — {state}. Reset: {resets_at}"
    key = "hard_stop_reverted" if did_revert else "hard_stop_clean"
    return i18n.msg(lang, key, label=label, pct=f"{pct:.0f}", resets_at=resets_at)


# --------------------------------------------------------------------------- chat auto-resume
#
# Lets a normal interactive chat continue on its own after the quota resets,
# without cc-run. See chat_resume.py for the mechanism. Everything here is
# fail-open: any error just means "no auto-resume", never a broken hook.

def _chat_enabled(cfg):
    return (
        chat_resume is not None
        and cfg.get("chat_auto_resume", True)
        and not _truthy(os.environ.get("CC_QUOTA_RUNNER", ""))  # cc-run resumes on its own
    )


def _resume_buffer():
    try:
        return max(0, int(os.environ.get("CC_RESUME_BUFFER", "60")))
    except ValueError:
        return 60


def _chat_msg(lang, key, fallback, **kw):
    if i18n is None:
        return fallback.format(**kw)
    return i18n.msg(lang, key, **kw)


def _chat_ping(text, tag):
    if notify is not None:
        notify.notify(text, title=f"cc-quota-guard: {tag}")


def _emit(payload):
    print(json.dumps(payload))
    sys.exit(0)


def _read_marker(proj):
    if not _quota_dir_is_safe(proj):
        return None
    try:
        with open(_marker_path(proj), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _same_session(stored, current):
    return not stored or not current or stored == current


def _chat_prepare_stop(proj, which, resets_at, lang, chat):
    """
    Extra marker fields, Claude-facing instructions and the human-facing
    suffix for a stop in chat mode. Returns ({}, "", "") when chat mode is
    off or anything goes wrong (plain stop, exactly as before).
    """
    if chat is None:
        return {}, "", ""
    try:
        plan = chat_resume.plan_resume(resets_at, which, buffer_seconds=_resume_buffer())
        if _quota_dir_is_safe(proj):
            chat_resume.archive_done_progress(_quota_dir(proj))
            chat_resume.clear_state(_quota_dir(proj))  # a new stop starts a new cycle
        extra = {
            "chat": True,
            "session_id": chat.get("session_id"),
            "resume_cron": plan["cron"],
            "resume_at": plan["fire_at"],
            "resume_display": plan["display"],
            "resume_scheduled": False,
            "resume_nudged": False,
        }
        suffix = " " + _chat_msg(lang, "chat_resume_suffix",
                                 "Auto-resume: {time} (keep this Claude Code window open).",
                                 time=plan["display"])
        return extra, chat_resume.schedule_instructions(plan), suffix
    except Exception:
        return {}, "", ""


def _chat_on_cron_created(proj, tool_input):
    """PostToolUse on CronCreate: remember that the wake-up exists."""
    if not chat_resume.is_resume_prompt((tool_input or {}).get("prompt")):
        return
    marker = _read_marker(proj)
    if marker and marker.get("chat"):
        marker["resume_scheduled"] = True
        _write_marker(proj, marker)


def _chat_on_resume(proj, cfg, session_id, marker, u=None):
    """The quota has reset and the chat is continuing: record it, ping 'quota reset'."""
    qd = _quota_dir(proj)
    if _quota_dir_is_safe(proj):
        os.makedirs(qd, exist_ok=True)
        _write_json_atomic(chat_resume.state_path(qd), {
            "resumed_at": int(time.time()),
            "session_id": session_id,
            "window": (marker or {}).get("window"),
        })
        try:
            os.remove(chat_resume.working_flag_path(qd))
        except OSError:
            pass
    lang = cfg["language"]
    which = (marker or {}).get("window") or "session"
    pct = ((u or {}).get(which) or {}).get("pct")
    if pct is not None:
        text = _chat_msg(lang, "chat_quota_reset",
                         "🔋 Quota reset ({label} now {pct}%) — resuming the chat automatically.",
                         label=_label(which, lang), pct=f"{pct:.0f}")
    else:
        text = _chat_msg(lang, "chat_quota_reset_nopct", "🔋 Quota reset — resuming the chat automatically.")
    _chat_ping(text, "reset")


def _chat_maybe_working(proj, cfg, session_id):
    """First tool call of a resumed turn: ping 'working again' (once)."""
    if not _quota_dir_is_safe(proj):
        return
    qd = _quota_dir(proj)
    if chat_resume.read_state(qd, session_id) is None:
        return
    if chat_resume.claim_once(chat_resume.working_flag_path(qd)):
        _chat_ping(_chat_msg(cfg["language"], "chat_working",
                             "▶️ Claude is working again, continuing where it left off."), "resumed")


def _handle_prompt(proj, hook_input, cfg):
    """UserPromptSubmit. Only acts on the auto-resume prompt our cron submits."""
    if not chat_resume.is_resume_prompt(hook_input.get("prompt")) or not _quota_dir_is_safe(proj):
        _allow()
    session_id = hook_input.get("session_id")
    marker = _read_marker(proj) or {}
    try:
        u = usage.get_usage(use_cache=False) if usage is not None else None
    except Exception:
        u = None  # fail-open: can't tell, so assume the reset happened and continue

    # Woke up too early (clock skew, reset moved, another window still over
    # its threshold): don't work, just schedule another wake-up.
    for which, thr_key in (("session", "session_soft"), ("week_all", "weekly_soft"),
                           ("week_sonnet", "weekly_soft")):
        block = (u or {}).get(which) or {}
        pct = block.get("pct")
        if pct is None or pct < cfg[thr_key]:
            continue
        plan = chat_resume.plan_resume(block.get("resets_at"), which, buffer_seconds=_resume_buffer())
        resets_at = block.get("resets_at") or datetime.fromtimestamp(plan["fire_at"], timezone.utc).isoformat()
        marker.update({
            "mode": marker.get("mode", "soft"), "window": which, "pct": pct, "resets_at": resets_at,
            "threshold": cfg[thr_key], "stopped_at": int(time.time()), "chat": True,
            "session_id": session_id, "resume_cron": plan["cron"], "resume_at": plan["fire_at"],
            "resume_display": plan["display"], "resume_scheduled": False, "resume_nudged": False,
        })
        _write_marker(proj, marker)
        _emit({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": chat_resume.not_reset_instructions(plan, _label(which, "en"), pct),
        }})

    try:
        os.remove(_marker_path(proj))
    except OSError:
        pass
    _chat_on_resume(proj, cfg, session_id, marker, u)
    _allow()


def _handle_stop(proj, hook_input, cfg):
    """
    Stop (end of every turn). Two jobs:
      - right after a chat-mode stop: make sure the wake-up cron exists
        (nudge Claude once, then warn the human if it still doesn't);
      - at the end of a resumed turn: send the "done" notification.
    """
    if not _quota_dir_is_safe(proj):
        _allow()
    qd = _quota_dir(proj)
    session_id = hook_input.get("session_id")
    marker = _read_marker(proj) if _marker_still_valid(proj) else None

    if marker and marker.get("chat") and _same_session(marker.get("session_id"), session_id):
        chat_resume.clear_state(qd)  # this turn ended on a (new) stop, it isn't "done"
        if marker.get("resume_scheduled") or chat_resume.crons_have_resume(hook_input.get("session_crons")):
            if not marker.get("resume_scheduled"):
                marker["resume_scheduled"] = True
                _write_marker(proj, marker)
            _allow()
        plan = {"cron": marker.get("resume_cron"), "display": marker.get("resume_display"),
                "fire_at": marker.get("resume_at")}
        if not plan["cron"]:
            plan = chat_resume.plan_resume(marker.get("resets_at"), marker.get("window") or "session",
                                           buffer_seconds=_resume_buffer())
        if not marker.get("resume_nudged") and not hook_input.get("stop_hook_active"):
            marker["resume_nudged"] = True
            _write_marker(proj, marker)
            _emit({"decision": "block", "reason": chat_resume.nudge_instructions(plan)})
        if not marker.get("schedule_failed_notified"):
            marker["schedule_failed_notified"] = True
            _write_marker(proj, marker)
            reset = chat_resume.parse_reset(marker.get("resets_at"))
            _chat_ping(_chat_msg(cfg["language"], "chat_schedule_failed",
                                 "⚠️ Couldn't schedule the automatic resume — type 'continue' in the chat "
                                 "after the quota resets ({time}).",
                                 time=chat_resume.fmt_local(reset) if reset else "?"), "warning")
        _allow()

    state = chat_resume.read_state(qd, session_id)
    if state is None:
        _allow()
    done = chat_resume.progress_done_since(qd, state.get("resumed_at", 0))
    if done:
        text = _chat_msg(cfg["language"], "chat_done", "✅ Done — Claude finished the resumed task.")
    else:
        text = _chat_msg(cfg["language"], "chat_turn_done",
                         "✅ Claude finished its resumed turn — check the chat for the result.")
    chat_resume.clear_state(qd)
    _chat_ping(text, "done")
    _allow()


def _handle_stop_failure(proj, hook_input, cfg):
    """StopFailure: the real usage limit hit before the guard could step in.
    Claude can't schedule anything without API access, so just tell the human
    (at most once per 30 minutes per project)."""
    if hook_input.get("error") != "rate_limit" or not _quota_dir_is_safe(proj):
        _allow()
    os.makedirs(_quota_dir(proj), exist_ok=True)
    flag = os.path.join(_quota_dir(proj), "limit_error.flag")
    try:
        if not os.path.islink(flag) and time.time() - os.path.getmtime(flag) < 1800:
            _allow()
    except OSError:
        pass
    try:
        _write_json_atomic(flag, int(time.time()))  # replaces a symlinked flag, never follows it
    except OSError:
        pass
    _chat_ping(_chat_msg(cfg["language"], "chat_limit_error",
                         "⛔ Claude stopped on a usage-limit error before the guard could step in — "
                         "type 'continue' in the chat after the quota resets."), "limit")
    _allow()


# --------------------------------------------------------------------------- hard threshold

def _sanitize_state_for_revert(proj, state, is_git):
    """
    Refuses to trust an in_progress claim in `state` (as read from
    todos_state.json) for hard-abort revert purposes if that file is
    itself tracked by git.

    todos_state.json is meant to be local runtime state — the README
    tells users to .gitignore `.cc-quota/` — so a *tracked* copy is a sign
    it shipped with the repo rather than being written by this hook. A
    cloned/untrusted repo could otherwise plant a fake in_progress entry
    so that the very first tool call in that project (before Claude has
    ever called TodoWrite there) sees hard_abort_enabled + an in_progress
    item + is_git all true, and runs `git stash` on whatever uncommitted
    work already happens to be sitting in that project's working tree —
    entirely unrelated to anything Claude did. This check only costs a
    `git ls-files` call, and only when we're actually about to act on an
    in_progress claim (i.e. only near a hard threshold), not on every call.
    """
    if not state or not state.get("in_progress") or not is_git:
        return state
    if _is_git_tracked(proj, ".cc-quota/todos_state.json"):
        return dict(state, in_progress=None)
    return state


def _do_hard_stop(proj, which, pct, resets_at, thr, state, is_git, hard_abort_enabled, lang="en", chat=None):
    in_progress = (state or {}).get("in_progress")
    # Falls back to a placeholder rather than None: a task recorded with no
    # subject (e.g. a TaskCreate call whose tool_response/tool_input both
    # omitted it — see _save_task_tool_state) would otherwise leave
    # aborted_item as None here, and aborted_item[:60] below would raise
    # TypeError — crashing the hook mid hard-abort, the one path this
    # project's fail-open design can least afford to break.
    aborted_item = (in_progress.get("content") if in_progress else None) or "(unnamed item)"
    stash_created = False
    did_revert = False

    if hard_abort_enabled and in_progress and is_git:
        did_revert = True
        stamp = int(time.time())
        code, out, _err = _git(
            proj, "stash", "push", "-u", "-m",
            f"cc-quota-guard: abort '{aborted_item[:60]}' @ {stamp}",
        )
        stash_created = code == 0 and "No local changes to save" not in out

        todos = state.get("todos", [])
        for t in todos:
            if t.get("content") == aborted_item:
                t["status"] = "pending"
        _write_todos_state(proj, {"todos": todos, "in_progress": None, "updated_at": stamp})

        _append_progress_note(
            proj,
            "## ⛔ HARD quota threshold — item reverted\n"
            f"- Time: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"- Reverted item: {aborted_item}\n"
            "- Method: git stash"
            + (" (stash created, recoverable with `git stash list`)"
               if stash_created else " (working tree was already clean, no stash created)")
            + "\n"
            f"- Checkpoint commit: {in_progress.get('checkpoint_commit') or 'unknown'}\n"
            "- After the reset, this item must be redone FROM SCRATCH.\n",
        )
    elif in_progress and not hard_abort_enabled:
        _append_progress_note(
            proj,
            "## ⚠️ HARD quota threshold — auto-revert is disabled\n"
            f"- Item in progress: {aborted_item}\n"
            "- `hard_abort_enabled` is off (this is the default), so nothing was reverted "
            "automatically. Leave this item in the safest state you can, commit if possible, "
            "and stop.\n",
        )
    elif in_progress and not is_git:
        _append_progress_note(
            proj,
            "## ⚠️ HARD quota threshold — no git, could not revert\n"
            f"- Item: {aborted_item}\n"
            "- This project is not a git repository; automatic revert was skipped. "
            "Leave the item in a safe state and note it manually.\n",
        )

    chat_extra, chat_reason, chat_suffix = _chat_prepare_stop(proj, which, resets_at, lang, chat)
    marker = {
        "mode": "hard",
        "window": which,
        "pct": pct,
        "resets_at": resets_at,
        "threshold": thr,
        "stopped_at": int(time.time()),
        "aborted_item": aborted_item if did_revert else None,
        "stash_created": stash_created,
        "git_available": is_git,
        "hard_abort_enabled": hard_abort_enabled,
    }
    marker.update(chat_extra)
    _write_marker(proj, marker)

    label = _label(which, "en")  # `reason` (Claude-facing) always stays English
    if did_revert:
        reason = (
            f"[HARD QUOTA THRESHOLD] {label} usage reached {pct:.0f}% (threshold {thr:.0f}%). "
            f"ALL changes made on '{aborted_item}' were REVERTED with `git stash` "
            "(the working tree is back to the commit from before this item started). NOW:\n"
            "1) Do NOT run any more edit/write/bash commands.\n"
            "2) Do NOT commit — the revert already happened, there is nothing to commit.\n"
            "3) Mark this item as 'pending' with TodoWrite "
            "(already recorded as pending in `.cc-quota/todos_state.json`).\n"
            f"4) Give a short note: '{aborted_item}' was reverted due to quota, will be redone from scratch after reset.\n"
            "5) END THE TURN — don't call any more tools.\n"
            f"Quota reset time: {resets_at}."
        )
    else:
        reason = (
            f"[HARD QUOTA THRESHOLD] {label} usage reached {pct:.0f}% (threshold {thr:.0f}%) — "
            "automatic revert was not performed (auto-revert is disabled, git is unavailable, or no item "
            "is in progress). Leave the current work in the safest possible state, commit if you can, "
            "give a short note, and END THE TURN. Don't start anything new.\n"
            f"Quota reset time: {resets_at}."
        )
    user_msg = _hard_user_msg(lang, _label(which, lang), pct, resets_at, did_revert)
    _block(reason + chat_reason, user_msg + chat_suffix)


# --------------------------------------------------------------------------- soft threshold

def _do_soft_stop(proj, which, pct, resets_at, thr, lang="en", chat=None):
    chat_extra, chat_reason, chat_suffix = _chat_prepare_stop(proj, which, resets_at, lang, chat)
    marker = {
        "mode": "soft",
        "window": which,
        "pct": pct,
        "resets_at": resets_at,
        "threshold": thr,
        "stopped_at": int(time.time()),
        "aborted_item": None,
        "stash_created": False,
    }
    marker.update(chat_extra)
    _write_marker(proj, marker)

    label = _label(which, "en")  # `reason` (Claude-facing) always stays English
    reason = (
        f"[QUOTA THRESHOLD] {label} usage reached {pct:.0f}% (threshold {thr:.0f}%). "
        "Do NOT start a new todo item. Instead, wrap up cleanly IN THIS ORDER:\n"
        "1) Update `.cc-quota/progress.md`: which items are done, what's next, what context/notes "
        "are needed to resume (read the file first, create it if missing).\n"
        "2) Don't leave any edit half-finished; leave the current item at a safe point.\n"
        "3) Commit your changes with a meaningful message (if this is a git repo).\n"
        "4) Give a short summary and END THE TURN — don't call any more tools.\n"
        f"Quota reset time: {resets_at}. After the reset, work resumes by reading `.cc-quota/progress.md`."
    )
    user_msg = _soft_user_msg(lang, _label(which, lang), pct, resets_at)
    _block(reason + chat_reason, user_msg + chat_suffix)


# --------------------------------------------------------------------------- main

def main():
    raw_in = sys.stdin.read()
    try:
        hook_input = json.loads(raw_in) if raw_in.strip() else {}
    except Exception:
        hook_input = {}

    proj = _project_dir(hook_input)
    cfg = _load_config(proj)
    chat_on = _chat_enabled(cfg)
    session_id = hook_input.get("session_id")

    # The same script is registered for UserPromptSubmit / Stop / StopFailure
    # too; those only drive chat auto-resume (see chat_resume.py).
    event = hook_input.get("hook_event_name") or "PostToolUse"
    if event != "PostToolUse":
        if chat_on:
            try:
                if event == "UserPromptSubmit":
                    _handle_prompt(proj, hook_input, cfg)
                elif event == "Stop":
                    _handle_stop(proj, hook_input, cfg)
                elif event == "StopFailure":
                    _handle_stop_failure(proj, hook_input, cfg)
            except Exception:
                pass  # fail-open
        _allow()

    tool_name = hook_input.get("tool_name") or hook_input.get("tool") or ""
    tool_input = hook_input.get("tool_input") or {}
    tool_response = hook_input.get("tool_response") or {}

    # Checked before the marker short-circuit below: the wake-up is scheduled
    # while the stop marker is still valid.
    if chat_on and tool_name == "CronCreate":
        try:
            _chat_on_cron_created(proj, tool_input)
        except Exception:
            pass

    # Already stopped and still within that stop's quota window — don't
    # block repeatedly and create a loop while cc-run is asleep waiting for
    # the reset. A marker that's stale (past its resets_at) or foreign
    # (shipped with the project instead of written by this run) is ignored
    # and removed instead of trusted — see _marker_still_valid(). Skipped
    # entirely if `.cc-quota` isn't a safe (real, non-symlinked) directory —
    # see _quota_dir_is_safe() — so we never touch a path that could resolve
    # outside the project.
    if _quota_dir_is_safe(proj) and os.path.exists(_marker_path(proj)):
        if _marker_still_valid(proj):
            _allow()
        stale = _read_marker(proj)
        try:
            os.remove(_marker_path(proj))
            removed = True
        except Exception:
            removed = False  # a parallel hook got there first
        # Fallback for the resume notification: the quota window passed and
        # this chat is working again, but UserPromptSubmit didn't announce it
        # (older Claude Code, or the user typed "continue" themselves).
        if removed and chat_on and stale and stale.get("chat") \
                and _same_session(stale.get("session_id"), session_id):
            try:
                try:
                    u_now = usage.get_usage() if usage is not None else None
                except Exception:
                    u_now = None
                _chat_on_resume(proj, cfg, session_id, stale, u_now)
            except Exception:
                pass

    if chat_on:
        try:
            _chat_maybe_working(proj, cfg, session_id)
        except Exception:
            pass
    chat = {"session_id": session_id} if chat_on else None

    # Lazy + memoized rather than computed unconditionally: _is_git_repo()
    # spawns a git subprocess, and most tool calls (a plain Edit/Bash/Read,
    # comfortably under every threshold) never actually need the answer —
    # only the TodoWrite/Task* state-tracking branch below and the HARD
    # checks further down do. Memoized so the handful of call sites that DO
    # need it within a single hook invocation still only spawn git once, same
    # as the old unconditional version did.
    _is_git_cache = {}

    def is_git():
        if "v" not in _is_git_cache:
            _is_git_cache["v"] = _is_git_repo(proj)
        return _is_git_cache["v"]

    # Some Claude Code versions plan with TaskCreate/TaskUpdate/TaskList
    # instead of TodoWrite. If this only ever recognized TodoWrite, the SOFT
    # threshold and in_progress bookkeeping would silently never fire on
    # those versions — see _save_task_tool_state()'s docstring.
    state = _read_todos_state(proj)
    if tool_name == "TodoWrite":
        todos = tool_input.get("todos") or []
        if todos:
            state = _save_todos_state(proj, todos, is_git())
    elif tool_name in ("TaskCreate", "TaskUpdate", "TaskList"):
        state = _save_task_tool_state(proj, tool_name, tool_input, tool_response, is_git())

    if usage is None:
        _allow()  # fail-open: module couldn't be imported
    try:
        u = usage.get_usage()
    except Exception:
        _allow()  # fail-open: couldn't read usage

    session = u.get("session") or {}
    week = u.get("week_all") or {}
    # Some accounts/plans also get a Sonnet-specific 7-day figure, separate
    # from the all-models week_all total — a heavy-Sonnet user could burn
    # through *that* cap while week_all still looks comfortably low, which
    # week_all alone would never catch. Checked against the exact same
    # weekly_soft/weekly_hard thresholds as week_all (no separate config
    # knob — this is a safety net on existing settings, not a new setting).
    # Absent on plans/responses that don't report it (pct stays None), same
    # as any other missing field here — the existing `is not None` guards
    # already handle that.
    week_sonnet = u.get("week_sonnet") or {}
    s_pct = session.get("pct")
    w_pct = week.get("pct")
    ws_pct = week_sonnet.get("pct")

    # HARD threshold — checked on every call, takes priority over soft.
    # state is re-sanitized right before use: an in_progress claim is only
    # honored for revert purposes if todos_state.json isn't itself tracked
    # by git (see _sanitize_state_for_revert()).
    if s_pct is not None and s_pct >= cfg["session_hard"]:
        _do_hard_stop(proj, "session", s_pct, session.get("resets_at"), cfg["session_hard"],
                      _sanitize_state_for_revert(proj, state, is_git()), is_git(), cfg["hard_abort_enabled"],
                      cfg["language"], chat=chat)
    if w_pct is not None and w_pct >= cfg["weekly_hard"]:
        _do_hard_stop(proj, "week_all", w_pct, week.get("resets_at"), cfg["weekly_hard"],
                      _sanitize_state_for_revert(proj, state, is_git()), is_git(), cfg["hard_abort_enabled"],
                      cfg["language"], chat=chat)
    if ws_pct is not None and ws_pct >= cfg["weekly_hard"]:
        _do_hard_stop(proj, "week_sonnet", ws_pct, week_sonnet.get("resets_at"), cfg["weekly_hard"],
                      _sanitize_state_for_revert(proj, state, is_git()), is_git(), cfg["hard_abort_enabled"],
                      cfg["language"], chat=chat)

    # SOFT thresholds — only checked at item boundaries: a TodoWrite call, or
    # (on Claude Code versions that plan this way instead) a
    # TaskCreate/TaskUpdate/TaskList call. Session, weekly, and weekly
    # (Sonnet) are independent tiers, same as their HARD counterparts
    # above; any one of them can fire on its own.
    if tool_name in ("TodoWrite", "TaskCreate", "TaskUpdate", "TaskList"):
        if s_pct is not None and s_pct >= cfg["session_soft"]:
            _do_soft_stop(proj, "session", s_pct, session.get("resets_at"), cfg["session_soft"], cfg["language"],
                          chat=chat)
        if w_pct is not None and w_pct >= cfg["weekly_soft"]:
            _do_soft_stop(proj, "week_all", w_pct, week.get("resets_at"), cfg["weekly_soft"], cfg["language"],
                          chat=chat)
        if ws_pct is not None and ws_pct >= cfg["weekly_soft"]:
            _do_soft_stop(proj, "week_sonnet", ws_pct, week_sonnet.get("resets_at"), cfg["weekly_soft"],
                          cfg["language"], chat=chat)

    _allow()


if __name__ == "__main__":
    main()
