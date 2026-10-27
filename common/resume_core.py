"""
resume_core.py — the parts of "continue the chat after the quota resets"
that are the same for every host (Claude Code, Codex CLI, Antigravity CLI).

Each host's hook imports this module and adds only what differs:
how the wake-up is scheduled (Claude: CronCreate tool, Antigravity:
`schedule` tool, Codex: a background watcher that runs `codex queue`),
and which state directory / done marker it uses.

Everything here is standard library only and fail-open: a helper that
can't do its job returns None/False instead of raising into a hook.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

# One-shot timers that land on :00 or :30 can fire up to ~90 s early
# (Claude Code's documented scheduler jitter). Those minutes are skipped so
# a wake-up never fires before the reset.
JITTER_MINUTES = (0, 30)

# A "resumed turn is running" record older than this is ignored (the window
# was closed, the machine slept for a day, ...) so it can't produce a late
# "done" notification.
STATE_MAX_AGE = 12 * 3600

# Shared, per-user settings for all three hosts (notification URL, language).
# Lives in the user's home directory, never in a project, so a cloned repo
# can't redirect notifications. Host-specific env vars / plugin options win.
USER_CONFIG = os.path.join(os.path.expanduser("~"), ".quota-guard", "settings.json")


# --------------------------------------------------------------------------- time

def parse_reset(resets_at):
    """ISO 8601 string or epoch number -> epoch seconds, else None."""
    if resets_at is None or resets_at == "":
        return None
    if isinstance(resets_at, (int, float)):
        return float(resets_at)
    try:
        dt = datetime.fromisoformat(str(resets_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def fmt_local(epoch, now=None):
    """Short local time for people: '03:42', or '30.10 03:42' when not today."""
    try:
        dt = datetime.fromtimestamp(float(epoch))
    except Exception:
        return str(epoch)
    today = datetime.fromtimestamp(now if now is not None else time.time()).date()
    return dt.strftime("%H:%M") if dt.date() == today else dt.strftime("%d.%m %H:%M")


def plan_resume(resets_at, which="session", now=None, buffer_seconds=60):
    """
    When to wake the chat up. Returns {"fire_at", "cron", "display", "seconds"}.

    fire_at = reset + buffer, rounded UP to a whole minute (cron has minute
    resolution), never in the past, never on :00/:30. A missing or
    unparsable reset falls back to 5 h (session) / 1 h (weekly) — the resume
    turn re-checks usage anyway and re-schedules if it's still too early.
    """
    now = time.time() if now is None else now
    reset = parse_reset(resets_at)
    if reset is None:
        reset = now + (18000 if which == "session" else 3600)
    fire = max(reset + max(0, buffer_seconds), now + 60)
    fire = (int(fire) // 60 + (1 if fire % 60 else 0)) * 60
    dt = datetime.fromtimestamp(fire)
    while dt.minute in JITTER_MINUTES:
        fire += 60
        dt = datetime.fromtimestamp(fire)
    cron = f"{dt.minute} {dt.hour} {dt.day} {dt.month} *"
    return {"fire_at": int(fire), "cron": cron, "display": fmt_local(fire, now),
            "seconds": max(60, int(fire - now))}


# --------------------------------------------------------------------------- files

def write_json_atomic(path, data):
    """A fresh, randomly named temp file (mkstemp opens with O_EXCL, so it
    never writes through anything that already exists) + os.replace(), which
    swaps `path` itself instead of following it. A fixed name like
    'STOP.json.tmp' is not safe: a cloned repo could commit it as a symlink
    to e.g. ~/.bashrc and a plain open(..., 'w') would overwrite the target."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".json")
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


def write_text_atomic(path, text):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json(path):
    """The JSON object at `path`, or {} (missing, symlink, not an object, ...)."""
    try:
        if os.path.islink(path):
            return {}
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def claim_once(path):
    """Atomically create `path`; True only for the first caller. Parallel
    hooks can't both send the same notification this way."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return False
    os.close(fd)
    return True


# --------------------------------------------------------------------------- resume state

def state_path(quota_dir):
    return os.path.join(quota_dir, "chat_resume.json")


def working_flag_path(quota_dir):
    return os.path.join(quota_dir, "chat_resume.working")


def read_state(quota_dir, session_id=None, now=None):
    """The 'a resumed turn is running' record, or None if absent/stale/foreign."""
    now = time.time() if now is None else now
    state = read_json(state_path(quota_dir))
    if not state:
        return None
    try:
        age = now - float(state.get("resumed_at", 0))
    except (TypeError, ValueError):
        return None
    if not 0 <= age <= STATE_MAX_AGE:
        return None
    if session_id and state.get("session_id") and state["session_id"] != session_id:
        return None
    return state


def write_state(quota_dir, session_id, **extra):
    data = dict(extra, resumed_at=int(time.time()), session_id=session_id)
    write_json_atomic(state_path(quota_dir), data)
    try:
        os.remove(working_flag_path(quota_dir))
    except OSError:
        pass
    return data


def clear_state(quota_dir):
    for path in (state_path(quota_dir), working_flag_path(quota_dir)):
        try:
            os.remove(path)
        except OSError:
            pass


def progress_done_since(quota_dir, since, done_line):
    """True if progress.md has a `done_line` line written at/after `since`."""
    path = os.path.join(quota_dir, "progress.md")
    try:
        if os.path.islink(path) or os.path.getmtime(path) + 1 < float(since):
            return False
        with open(path, encoding="utf-8") as f:
            return any(line.strip() == done_line for line in f)
    except Exception:
        return False


def archive_done_progress(quota_dir, done_line):
    """A new stop starts a new cycle: a leftover done marker from an earlier
    finished task would make the resume turn think it's already done."""
    path = os.path.join(quota_dir, "progress.md")
    try:
        if os.path.islink(path):
            return
        with open(path, encoding="utf-8") as f:
            if not any(line.strip() == done_line for line in f):
                return
        os.replace(path, path + ".done-" + time.strftime("%Y%m%d%H%M%S"))
    except Exception:
        pass


# --------------------------------------------------------------------------- settings

def user_config():
    """~/.quota-guard/settings.json, or {}. Path overridable for tests."""
    return read_json(os.environ.get("QUOTA_GUARD_CONFIG") or USER_CONFIG)


def truthy(value, default=True):
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- processes

def spawn_detached(argv, log_path=None, env=None):
    """Start `argv` so it outlives the hook that started it (own session /
    process group, no console window, no inherited pipes). Returns the PID,
    or None if it couldn't be started."""
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  close_fds=True, env=env)
    log = None
    if log_path:
        try:
            log = open(log_path, "a", encoding="utf-8")
            kwargs.update(stdout=log, stderr=log)
        except OSError:
            log = None
    try:
        if os.name == "nt":
            detached, new_group, no_window, breakaway = 0x00000008, 0x00000200, 0x08000000, 0x01000000
            try:
                # Leave the host's job object too, if it allows that, so closing
                # the hook's job can't take the watcher down with it.
                proc = subprocess.Popen(argv, creationflags=detached | new_group | no_window | breakaway, **kwargs)
            except OSError:
                proc = subprocess.Popen(argv, creationflags=detached | new_group | no_window, **kwargs)
        else:
            proc = subprocess.Popen(argv, start_new_session=True, **kwargs)
        return proc.pid
    except OSError:
        return None
    finally:
        if log:
            log.close()


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                                 text=True, timeout=10, stdin=subprocess.DEVNULL).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def python_exe():
    return sys.executable or "python3"
