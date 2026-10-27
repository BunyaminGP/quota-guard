"""Chat auto-resume for normal interactive Antigravity chats (no agy-run).

stop -> the agent is told to set a `schedule` timer -> the timer fires (the
schedule tool's PostToolUse) -> quota re-checked -> "quota reset" ping and the
chat continues -> first tool call "working again" -> end of turn "done".
"""
from __future__ import annotations

import http.server
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_quota_gate import _make_usage_fixture, _run_gate

CONV = "conv-chat-1"
TAG = "[agy-quota-guard:auto-resume]"


def _iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


class Pings(list):
    url = ""


@pytest.fixture
def pings():
    received = Pings()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
            received.append({"title": self.headers.get("Title"), "body": body})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    received.url = f"http://127.0.0.1:{server.server_port}/topic"
    try:
        yield received
    finally:
        server.shutdown()


def _env(tmp_path: Path, pings, used: float = 0.85, reset_in: int = 3600, **extra) -> dict:
    fixture = _make_usage_fixture(tmp_path, [
        {"id": "gemini-5h", "window": "5h", "remaining_fraction": round(1 - used, 4), "reset_time": _iso(reset_in)},
        {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.5, "reset_time": _iso(86400)},
    ], name=f"usage-{int(used * 100)}.json")
    env = {"AGY_USAGE_FIXTURE": str(fixture), "AGY_LANG": "tr", "AGY_NOTIFY_URL": pings.url, "AGY_RESUME_BUFFER": "0"}
    env.update(extra)
    return env


def _payload(tmp_path: Path, conv: str | None = CONV, tool: str | None = None, args: dict | None = None) -> dict:
    p = {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro", "invocationNum": 1}
    if conv:
        p["conversationId"] = conv
    if tool:
        p["toolCall"] = {"name": tool, "args": args or {}}
    return p


def _schedule_args() -> dict:
    return {"DurationSeconds": 3660, "Prompt": TAG + " The usage quota has reset. Continue ..."}


def _marker(tmp_path: Path) -> dict:
    return json.loads((tmp_path / ".agy-quota" / "STOP.json").read_text(encoding="utf-8"))


def _expired_chat_marker(tmp_path: Path, conv: str = CONV) -> None:
    qdir = tmp_path / ".agy-quota"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "STOP.json").write_text(json.dumps({
        "mode": "soft", "window": "gemini-5h", "window_label": "gemini-5h (session)", "pct": 85.0,
        "threshold": 80.0, "resets_at": _iso(-60), "chat": True, "conversation_id": conv,
        "resume_scheduled": True, "checkpoint_saved": True, "soft_prompted": True,
    }), encoding="utf-8")


# --------------------------------------------------------------------------- the stop

def test_chat_stop_asks_for_a_schedule_timer_and_says_when(tmp_path, pings):
    out = _run_gate(tmp_path, "PreInvocation", _payload(tmp_path), _env(tmp_path, pings))
    reason = out["injectSteps"][0]["userMessage"]
    assert "AUTO-RESUME" in reason and "`schedule`" in reason and TAG in reason
    marker = _marker(tmp_path)
    assert marker["chat"] is True and marker["conversation_id"] == CONV
    assert marker["resume_at"] >= datetime.now(timezone.utc).timestamp() + 3500
    assert len(pings) == 1 and pings[0]["title"] == "agy-quota-guard SOFT STOP"
    assert "Otomatik devam: " + marker["resume_display"] in pings[0]["body"]


def test_schedule_call_is_allowed_and_the_turn_may_end_on_its_own(tmp_path, pings):
    env = _env(tmp_path, pings)
    _run_gate(tmp_path, "PreInvocation", _payload(tmp_path), env)
    other = _run_gate(tmp_path, "PreToolUse", _payload(tmp_path, tool="run_command", args={"CommandLine": "pytest"}), env)
    assert other["decision"] == "deny"
    sched = _run_gate(tmp_path, "PreToolUse", _payload(tmp_path, tool="schedule", args=_schedule_args()), env)
    assert sched == {"decision": "allow"} and _marker(tmp_path)["resume_scheduled"] is True
    # no "terminate" while the timer is set, so it can't cancel it; bounded to a few calls
    results = [_run_gate(tmp_path, "PostInvocation", _payload(tmp_path), env) for _ in range(4)]
    assert results[:3] == [{}, {}, {}] and results[3]["terminationBehavior"] == "terminate"
    assert _run_gate(tmp_path, "Stop", _payload(tmp_path), env) == {}
    assert len(pings) == 1  # just the stop itself, no warning


def test_missing_timer_gets_one_nudge_then_a_warning_ping(tmp_path, pings):
    env = _env(tmp_path, pings)
    _run_gate(tmp_path, "PreInvocation", _payload(tmp_path), env)
    first = _run_gate(tmp_path, "PostInvocation", _payload(tmp_path), env)
    assert first["terminationBehavior"] == "force_continue" and "AUTO-RESUME" in first["injectSteps"][0]["userMessage"]
    assert _run_gate(tmp_path, "PostInvocation", _payload(tmp_path), env)["terminationBehavior"] == "terminate"
    _run_gate(tmp_path, "Stop", _payload(tmp_path), env)
    _run_gate(tmp_path, "Stop", _payload(tmp_path), env)
    assert [p["title"] for p in pings] == ["agy-quota-guard SOFT STOP", "agy-quota-guard WARNING"]
    assert "Otomatik devam zamanlanamadı" in pings[1]["body"]


@pytest.mark.parametrize("extra", [{"AGY_QUOTA_RUNNER": "1"}, {"AGY_CHAT_AUTO_RESUME": "0"}, "runner.json"])
def test_runner_or_opt_out_keeps_the_plain_stop(tmp_path, pings, extra):
    env = _env(tmp_path, pings)
    if extra == "runner.json":
        (tmp_path / ".agy-quota" / "runner.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    else:
        env.update(extra)
    out = _run_gate(tmp_path, "PreInvocation", _payload(tmp_path), env)
    assert "AUTO-RESUME" not in out["injectSteps"][0]["userMessage"]
    assert "chat" not in _marker(tmp_path)
    assert "Otomatik devam" not in pings[0]["body"]


def test_no_conversation_id_keeps_the_plain_stop(tmp_path, pings):
    out = _run_gate(tmp_path, "PreInvocation", _payload(tmp_path, conv=None), _env(tmp_path, pings))
    assert "AUTO-RESUME" not in out["injectSteps"][0]["userMessage"]


# --------------------------------------------------------------------------- the wake-up

def test_wake_up_after_reset_pings_then_working_then_done(tmp_path, pings):
    _expired_chat_marker(tmp_path)
    env = _env(tmp_path, pings, used=0.05, reset_in=18000)
    assert _run_gate(tmp_path, "PostToolUse", _payload(tmp_path, tool="schedule", args=_schedule_args()), env) == {}
    assert not (tmp_path / ".agy-quota" / "STOP.json").exists()
    state = json.loads((tmp_path / ".agy-quota" / "chat_resume.json").read_text(encoding="utf-8"))
    assert state["session_id"] == CONV
    assert pings[-1] == {"title": "agy-quota-guard RESET", "body": "[agy-quota-guard] 🔋 Kota yenilendi "
                         "(gemini-5h (session) şu an %5) — sohbet otomatik sürdürülüyor."}
    for _ in range(2):
        _run_gate(tmp_path, "PostToolUse", _payload(tmp_path, tool="run_command", args={"CommandLine": "ls"}), env)
    _run_gate(tmp_path, "PostToolUse", _payload(tmp_path, conv="someone-else", tool="run_command"), env)
    assert [p["title"] for p in pings] == ["agy-quota-guard RESET", "agy-quota-guard RESUMED"]
    assert pings[1]["body"].endswith("Antigravity kaldığı yerden çalışmaya devam ediyor.")
    (tmp_path / ".agy-quota" / "progress.md").write_text("done\nAGY_QUOTA_DONE\n", encoding="utf-8")
    _run_gate(tmp_path, "Stop", _payload(tmp_path), env)
    assert pings[-1]["title"] == "agy-quota-guard DONE" and pings[-1]["body"].endswith("Antigravity kaldığı işi tamamladı.")
    _run_gate(tmp_path, "Stop", _payload(tmp_path), env)
    assert len(pings) == 3  # the cycle is closed


def test_turn_without_done_line_says_turn_finished(tmp_path, pings):
    _expired_chat_marker(tmp_path)
    env = _env(tmp_path, pings, used=0.05, reset_in=18000)
    _run_gate(tmp_path, "PostToolUse", _payload(tmp_path, tool="schedule", args=_schedule_args()), env)
    _run_gate(tmp_path, "Stop", _payload(tmp_path), env)
    assert pings[-1]["body"].endswith("Antigravity devam turunu bitirdi — sonucu sohbetten kontrol et.")


def test_wake_up_while_still_over_threshold_asks_for_a_new_timer(tmp_path, pings):
    _expired_chat_marker(tmp_path)
    env = _env(tmp_path, pings, used=0.90, reset_in=1800)
    _run_gate(tmp_path, "PostToolUse", _payload(tmp_path, tool="schedule", args=_schedule_args()), env)
    marker = _marker(tmp_path)
    assert marker["chat"] is True and marker["resume_scheduled"] is False
    assert marker["resume_at"] >= datetime.now(timezone.utc).timestamp() + 1700
    assert pings == []  # silent: the user was already told about this stop
    out = _run_gate(tmp_path, "PreInvocation", _payload(tmp_path), env)
    assert "AUTO-RESUME" in out["injectSteps"][0]["userMessage"]


def test_wake_up_after_another_hook_cleaned_the_marker(tmp_path, pings):
    _expired_chat_marker(tmp_path)
    env = _env(tmp_path, pings, used=0.05, reset_in=18000)
    _run_gate(tmp_path, "PreInvocation", _payload(tmp_path), env)  # removes the expired STOP.json first
    assert not (tmp_path / ".agy-quota" / "STOP.json").exists()
    _run_gate(tmp_path, "PostToolUse", _payload(tmp_path, tool="schedule", args=_schedule_args()), env)
    assert "gemini-5h (session) şu an %5" in pings[-1]["body"]
    assert not (tmp_path / ".agy-quota" / "chat_wake.json").exists()


def test_wake_up_of_another_conversation_is_ignored(tmp_path, pings):
    _expired_chat_marker(tmp_path, conv="someone-else")
    env = _env(tmp_path, pings, used=0.05, reset_in=18000)
    _run_gate(tmp_path, "PostToolUse", _payload(tmp_path, tool="schedule", args=_schedule_args()), env)
    assert pings == [] and not (tmp_path / ".agy-quota" / "chat_resume.json").exists()


def test_shared_settings_give_url_and_language(tmp_path, pings, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"notify_url": pings.url, "language": "tr"}), encoding="utf-8")
    monkeypatch.setenv("QUOTA_GUARD_CONFIG", str(settings))
    env = _env(tmp_path, pings)
    del env["AGY_NOTIFY_URL"], env["AGY_LANG"]
    _run_gate(tmp_path, "PreInvocation", _payload(tmp_path), env)
    assert len(pings) == 1 and "Otomatik devam" in pings[0]["body"]


# --------------------------------------------------------------------------- safe writes

def test_planted_symlinks_are_replaced_not_followed(tmp_path, pings):
    qdir = tmp_path / ".agy-quota"
    qdir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    try:
        os.symlink(victim, qdir / "STOP.json")
        os.symlink(victim, qdir / "session.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    _run_gate(tmp_path, "PreInvocation", _payload(tmp_path), _env(tmp_path, pings))
    assert victim.read_text(encoding="utf-8") == "keep me"
