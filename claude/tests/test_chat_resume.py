"""
Tests for chat-mode auto-resume (scripts/chat_resume.py + the
UserPromptSubmit / Stop / StopFailure handling in quota_gate.py).

The hook is run as a real subprocess, exactly as Claude Code runs it, with
the quota coming from usage.py's simulation file and notifications caught by
a local HTTP server. A full cycle looks like this in a real chat:

  PostToolUse (over threshold) -> block + "stopped" ping, Claude told to CronCreate
  PostToolUse (CronCreate)     -> wake-up recorded
  Stop                         -> wake-up verified, turn ends
  ... quota resets, the cron fires ...
  UserPromptSubmit (tag)       -> "quota reset" ping
  PostToolUse                  -> "working again" ping (once)
  Stop                         -> "done" ping
"""

import http.server
import json
import os
import sys
import threading
import time
from datetime import datetime

import pytest

import chat_resume

from conftest import REPO_ROOT, run_subprocess

GATE = os.path.join(REPO_ROOT, "scripts", "quota_gate.py")
SESSION = "sess-chat-1"


class _Capture(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8")
        self.server.items.append({"title": self.headers.get("Title", ""), "body": body})
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def pings():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Capture)
    server.items = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)


@pytest.fixture
def env(tmp_path, pings):
    proj = tmp_path / "proj"
    proj.mkdir()
    e = {k: v for k, v in os.environ.items()
         if not k.startswith(("CC_", "CLAUDE_PLUGIN_OPTION_")) and k != "CLAUDE_PROJECT_DIR"}
    e.update(
        CLAUDE_PROJECT_DIR=str(proj),
        CC_QUOTA_SIM_FILE=str(tmp_path / "sim.json"),
        QUOTA_GUARD_CONFIG=str(tmp_path / "no-shared-settings.json"),
        CC_USAGE_CACHE=str(tmp_path / "cache.json"),
        CC_CREDENTIALS_FILE=str(tmp_path / "no-credentials.json"),
        CC_NOTIFY_URL=f"http://127.0.0.1:{pings.server_address[1]}/topic",
        CC_LANG="tr",
        PYTHONIOENCODING="utf-8",
    )
    return e


def simulate(env, pct, reset_in_seconds, after=3.0):
    now = time.time()
    with open(env["CC_QUOTA_SIM_FILE"], "w", encoding="utf-8") as f:
        json.dump({"pct": pct, "after_pct": after, "window": "session",
                   "resets_at": now + reset_in_seconds, "expires_at": now + reset_in_seconds + 1800}, f)


def hook(env, event, **fields):
    payload = {"hook_event_name": event, "session_id": SESSION, "cwd": env["CLAUDE_PROJECT_DIR"]}
    payload.update(fields)
    r = run_subprocess([sys.executable, GATE], input=json.dumps(payload), env=env)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip() else {}


def post_tool(env, tool="Bash", tool_input=None):
    return hook(env, "PostToolUse", tool_name=tool, tool_input=tool_input or {"command": "echo hi"})


def marker(env):
    path = os.path.join(env["CLAUDE_PROJECT_DIR"], ".cc-quota", "STOP.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def wait_past_reset(env):
    with open(env["CC_QUOTA_SIM_FILE"], encoding="utf-8") as f:
        reset = json.load(f)["resets_at"]
    time.sleep(max(0.0, reset - time.time()) + 0.3)


def titles(pings):
    return [p["title"] for p in pings.items]


# --------------------------------------------------------------------------- full cycle

def test_full_chat_cycle_sends_stop_reset_working_done(env, pings):
    simulate(env, 96, reset_in_seconds=2)

    out = post_tool(env)
    assert out["decision"] == "block"
    assert "CronCreate" in out["reason"] and chat_resume.RESUME_TAG in out["reason"]
    assert "Otomatik devam" in out["systemMessage"]
    m = marker(env)
    assert m["chat"] is True and m["session_id"] == SESSION and m["resume_scheduled"] is False
    assert titles(pings) == ["cc-quota-guard"]

    # Claude writes the checkpoint and schedules the wake-up (marker still valid -> no re-block)
    assert post_tool(env, "CronCreate", {"cron": m["resume_cron"], "recurring": False,
                                         "prompt": chat_resume.RESUME_PROMPT}) == {}
    assert marker(env)["resume_scheduled"] is True
    assert hook(env, "Stop", stop_hook_active=False, session_crons=[]) == {}
    assert titles(pings) == ["cc-quota-guard"]

    # ... the quota resets and the cron fires
    wait_past_reset(env)
    assert hook(env, "UserPromptSubmit", prompt=chat_resume.RESUME_PROMPT) == {}
    assert marker(env) is None
    assert titles(pings)[-1] == "cc-quota-guard: reset"
    assert "Kota yenilendi" in pings.items[-1]["body"] and "%3" in pings.items[-1]["body"]

    assert post_tool(env) == {}
    assert post_tool(env) == {}  # "working again" only once
    assert titles(pings).count("cc-quota-guard: resumed") == 1

    progress = os.path.join(env["CLAUDE_PROJECT_DIR"], ".cc-quota", "progress.md")
    with open(progress, "a", encoding="utf-8") as f:
        f.write("all done\nCC_QUOTA_DONE\n")
    assert hook(env, "Stop", stop_hook_active=False, session_crons=[]) == {}
    assert titles(pings) == ["cc-quota-guard", "cc-quota-guard: reset",
                             "cc-quota-guard: resumed", "cc-quota-guard: done"]
    assert "Bitti" in pings.items[-1]["body"]

    # the cycle is closed: later turns are quiet
    assert hook(env, "Stop", stop_hook_active=False) == {}
    assert len(pings.items) == 4


def test_done_without_marker_line_uses_turn_finished_text(env, pings):
    simulate(env, 96, reset_in_seconds=1)
    post_tool(env)
    post_tool(env, "CronCreate", {"prompt": chat_resume.RESUME_PROMPT})
    wait_past_reset(env)
    hook(env, "UserPromptSubmit", prompt=chat_resume.RESUME_PROMPT)
    hook(env, "Stop", stop_hook_active=False)
    assert titles(pings)[-1] == "cc-quota-guard: done"
    assert "devam turunu bitirdi" in pings.items[-1]["body"]


# --------------------------------------------------------------------------- making sure the wake-up exists

def test_stop_nudges_once_then_warns_when_cron_missing(env, pings):
    simulate(env, 96, reset_in_seconds=600)
    post_tool(env)
    first = hook(env, "Stop", stop_hook_active=False, session_crons=[])
    assert first["decision"] == "block" and "CronCreate" in first["reason"]
    assert marker(env)["resume_nudged"] is True
    assert hook(env, "Stop", stop_hook_active=True, session_crons=[]) == {}
    assert titles(pings) == ["cc-quota-guard", "cc-quota-guard: warning"]
    assert hook(env, "Stop", stop_hook_active=False, session_crons=[]) == {}  # warned once only
    assert len(pings.items) == 2


def test_stop_accepts_cron_seen_in_session_crons(env, pings):
    simulate(env, 96, reset_in_seconds=600)
    post_tool(env)
    crons = [{"id": "c1", "schedule": "5 4 1 1 *", "recurring": False, "prompt": chat_resume.RESUME_PROMPT}]
    assert hook(env, "Stop", stop_hook_active=False, session_crons=crons) == {}
    assert marker(env)["resume_scheduled"] is True


def test_cron_created_for_another_prompt_is_not_counted(env):
    simulate(env, 96, reset_in_seconds=600)
    post_tool(env)
    post_tool(env, "CronCreate", {"prompt": "remind me to drink water"})
    assert marker(env)["resume_scheduled"] is False


# --------------------------------------------------------------------------- waking up too early

def test_resume_prompt_before_reset_reschedules_without_ping(env, pings):
    simulate(env, 96, reset_in_seconds=600)
    post_tool(env)
    pings.items.clear()
    out = hook(env, "UserPromptSubmit", prompt=chat_resume.RESUME_PROMPT)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "NOT reset" in ctx and "CronCreate" in ctx
    assert pings.items == []
    m = marker(env)
    assert m["chat"] is True and m["resume_scheduled"] is False


def test_ordinary_prompt_is_ignored(env, pings):
    simulate(env, 96, reset_in_seconds=600)
    post_tool(env)
    pings.items.clear()
    assert hook(env, "UserPromptSubmit", prompt="hello there") == {}
    assert pings.items == [] and marker(env)["chat"] is True


# --------------------------------------------------------------------------- fallback without UserPromptSubmit

def test_first_tool_call_after_reset_announces_resume_when_prompt_hook_did_not(env, pings):
    simulate(env, 96, reset_in_seconds=1)
    post_tool(env)
    wait_past_reset(env)
    assert post_tool(env) == {}
    assert titles(pings) == ["cc-quota-guard", "cc-quota-guard: reset", "cc-quota-guard: resumed"]


def test_other_session_does_not_get_this_sessions_done(env, pings):
    simulate(env, 96, reset_in_seconds=1)
    post_tool(env)
    wait_past_reset(env)
    hook(env, "UserPromptSubmit", prompt=chat_resume.RESUME_PROMPT)
    count = len(pings.items)
    hook(env, "Stop", session_id="another-session", stop_hook_active=False)
    assert len(pings.items) == count


# --------------------------------------------------------------------------- switches

@pytest.mark.parametrize("var,value", [("CC_QUOTA_RUNNER", "1"), ("CC_CHAT_AUTO_RESUME", "0"),
                                       ("CLAUDE_PLUGIN_OPTION_CHAT_AUTO_RESUME", "false")])
def test_runner_or_disabled_keeps_the_old_plain_stop(env, pings, var, value):
    env[var] = value
    simulate(env, 96, reset_in_seconds=600)
    out = post_tool(env)
    assert out["decision"] == "block" and "CronCreate" not in out["reason"]
    assert "Otomatik devam" not in out["systemMessage"]
    assert "chat" not in marker(env)
    assert hook(env, "Stop", stop_hook_active=False, session_crons=[]) == {}
    assert titles(pings) == ["cc-quota-guard"]


def test_new_stop_archives_a_leftover_done_marker(env):
    qdir = os.path.join(env["CLAUDE_PROJECT_DIR"], ".cc-quota")
    os.makedirs(qdir)
    with open(os.path.join(qdir, "progress.md"), "w", encoding="utf-8") as f:
        f.write("old task\nCC_QUOTA_DONE\n")
    simulate(env, 96, reset_in_seconds=600)
    post_tool(env)
    assert not os.path.exists(os.path.join(qdir, "progress.md"))
    assert any(n.startswith("progress.md.done-") for n in os.listdir(qdir))


# --------------------------------------------------------------------------- real limit error

def test_rate_limit_stop_failure_pings_once(env, pings):
    hook(env, "StopFailure", error="rate_limit", last_assistant_message="API Error: Rate limit reached")
    hook(env, "StopFailure", error="rate_limit")
    hook(env, "StopFailure", error="server_error")
    assert titles(pings) == ["cc-quota-guard: limit"]


# --------------------------------------------------------------------------- untrusted .cc-quota/ contents

_OLD_TMP_NAMES = ("STOP.json.tmp", "todos_state.json.tmp", "chat_resume.json.tmp")


def _run_every_writer(env):
    simulate(env, 96, reset_in_seconds=1)
    post_tool(env, "TodoWrite", {"todos": [{"content": "a", "status": "in_progress"}]})
    post_tool(env, "CronCreate", {"prompt": chat_resume.RESUME_PROMPT})
    wait_past_reset(env)
    hook(env, "UserPromptSubmit", prompt=chat_resume.RESUME_PROMPT)
    hook(env, "StopFailure", error="rate_limit")


def test_writes_never_open_fixed_temp_names(env):
    """A cloned repo can ship files under .cc-quota/; the hook must not write
    through any fixed name it didn't create itself."""
    qdir = os.path.join(env["CLAUDE_PROJECT_DIR"], ".cc-quota")
    os.makedirs(qdir)
    for name in _OLD_TMP_NAMES:
        with open(os.path.join(qdir, name), "w", encoding="utf-8") as f:
            f.write("planted")
    _run_every_writer(env)
    for name in _OLD_TMP_NAMES:
        with open(os.path.join(qdir, name), encoding="utf-8") as f:
            assert f.read() == "planted", name
    assert os.path.exists(os.path.join(qdir, "chat_resume.json"))
    assert not [n for n in os.listdir(qdir) if n.startswith(".tmp-")]


def test_planted_symlinks_are_never_followed(env, tmp_path, has_symlink_support):
    if not has_symlink_support:
        pytest.skip("this platform/user can't create symlinks")
    victim = tmp_path / "victim.txt"
    victim.write_text("precious", encoding="utf-8")
    qdir = os.path.join(env["CLAUDE_PROJECT_DIR"], ".cc-quota")
    os.makedirs(qdir)
    for name in _OLD_TMP_NAMES + ("limit_error.flag",):
        os.symlink(str(victim), os.path.join(qdir, name))
    _run_every_writer(env)
    assert victim.read_text(encoding="utf-8") == "precious"


# --------------------------------------------------------------------------- planning

def test_plan_resume_rounds_up_and_skips_jitter_minutes():
    base = datetime(2026, 10, 27, 14, 29, 10).timestamp()
    plan = chat_resume.plan_resume(base, "session", now=base - 3600, buffer_seconds=60)
    fire = datetime.fromtimestamp(plan["fire_at"])
    assert fire.second == 0 and fire.minute not in (0, 30)
    assert plan["fire_at"] >= base + 60
    assert plan["cron"] == f"{fire.minute} {fire.hour} {fire.day} {fire.month} *"


def test_plan_resume_never_in_the_past_and_has_fallback():
    now = time.time()
    assert chat_resume.plan_resume(now - 999, now=now)["fire_at"] >= now + 60
    assert chat_resume.plan_resume(None, "session", now=now)["fire_at"] >= now + 18000
    assert chat_resume.plan_resume("not a date", "week_all", now=now)["fire_at"] >= now + 3600


# --------------------------------------------------------------------------- simulation

def test_simulation_flips_after_reset_and_expires(tmp_path, monkeypatch):
    import usage
    monkeypatch.setattr(usage, "SIM_FILE", str(tmp_path / "sim.json"))
    data = usage.write_simulation(96, reset_in_minutes=1, after_pct=4)
    reset = data["resets_at"]
    assert usage.read_simulation(now=reset - 1)["session"]["pct"] == 96
    assert usage.read_simulation(now=reset + 1)["session"]["pct"] == 4
    assert usage.get_usage()["simulated"] is True
    assert usage.read_simulation(now=reset + 1801) is None
    assert not os.path.exists(tmp_path / "sim.json")
