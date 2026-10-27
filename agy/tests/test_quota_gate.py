from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
GATE_SCRIPT = SCRIPTS_DIR / "quota_gate.py"


def _future_iso(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _past_iso(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _make_usage_fixture(tmp_path: Path, buckets: list[dict], name: str = "usage.json") -> Path:
    payload = {
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [b for b in buckets if str(b.get("id", "")).startswith("gemini-")],
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": [b for b in buckets if str(b.get("id", "")).startswith("3p-")],
                    },
                ]
            },
        }
    }
    qdir = tmp_path / ".agy-quota"
    qdir.mkdir(parents=True, exist_ok=True)
    f = qdir / name
    f.write_text(json.dumps(payload), encoding="utf-8")
    return f


def _run_gate(proj: Path, event: str, payload: dict, env_extra: dict | None = None) -> dict:
    env = os.environ.copy()
    for leaked in ("AGY_NOTIFY_URL", "AGY_PLUGIN_OPTION_NOTIFY_URL", "AGY_LANG", "AGY_PLUGIN_OPTION_LANGUAGE"):
        env.pop(leaked, None)
    env["AGY_QUOTA_CACHE_FILE"] = str(proj / ".agy-quota" / "test_cache.json")
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), "--event", event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(proj),
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}"
    return json.loads(proc.stdout.strip())


def _init_git_repo(proj: Path) -> None:
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
    ):
        subprocess.run(cmd, cwd=str(proj), check=True, capture_output=True, stdin=subprocess.DEVNULL)
    (proj / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(proj), check=True, capture_output=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=str(proj), check=True, capture_output=True, stdin=subprocess.DEVNULL)


def test_post_tool_use_always_returns_empty_json_and_tracks_task_boundary(tmp_path):
    _init_git_repo(tmp_path)
    fixture = _make_usage_fixture(
        tmp_path,
        [
            {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.50, "reset_time": _future_iso(1800)},
            {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.50, "reset_time": _future_iso(86400)},
        ],
    )
    out = _run_gate(
        tmp_path,
        "PostToolUse",
        {
            "conversationId": "conv-123",
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {
                "name": "task_boundary",
                "args": {"TaskName": "Implement feature A", "TaskStatus": "in_progress"},
            },
        },
        {"AGY_USAGE_FIXTURE": str(fixture)},
    )
    assert out == {}
    state = json.loads((tmp_path / ".agy-quota" / "todos_state.json").read_text(encoding="utf-8"))
    assert state["in_progress"]["content"] == "Implement feature A"
    assert state["in_progress"]["checkpoint_clean"] is True
    assert state["in_progress"]["checkpoint_commit"]
    sess = json.loads((tmp_path / ".agy-quota" / "session.json").read_text(encoding="utf-8"))
    assert sess["conversation_id"] == "conv-123"


def test_soft_stop_lifecycle_pre_invocation_pre_tool_use_and_post_invocation(tmp_path):
    reset_ts = _future_iso(3600)
    fixture = _make_usage_fixture(
        tmp_path,
        [
            {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.15, "reset_time": reset_ts},  # 85% used >= 80% soft
            {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.50, "reset_time": _future_iso(86400)},
        ],
    )
    env = {"AGY_USAGE_FIXTURE": str(fixture)}

    # 1. PreInvocation injects soft stop instructions to save checkpoint first
    pre_inv = _run_gate(
        tmp_path,
        "PreInvocation",
        {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro", "invocationNum": 1},
        env,
    )
    assert "injectSteps" in pre_inv
    assert "QUOTA SOFT STOP" in pre_inv["injectSteps"][0]["userMessage"]
    stop_file = tmp_path / ".agy-quota" / "STOP.json"
    assert stop_file.exists()

    # 2. PreToolUse denies regular tool calls (run_command) while STOP.json is active
    deny_res = _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest"}},
        },
        env,
    )
    assert deny_res["decision"] == "deny"

    # 3. PreToolUse ALLOWS writing the checkpoint to .agy-quota/progress.md
    allow_ckpt = _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {
                "name": "write_to_file",
                "args": {"TargetFile": str(tmp_path / ".agy-quota" / "progress.md")},
            },
        },
        env,
    )
    assert allow_ckpt == {"decision": "allow"}

    # 4. PostToolUse records checkpoint_saved=True when progress.md is written
    post_tool = _run_gate(
        tmp_path,
        "PostToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {
                "name": "write_to_file",
                "args": {"TargetFile": str(tmp_path / ".agy-quota" / "progress.md")},
            },
        },
        env,
    )
    assert post_tool == {}
    marker = json.loads(stop_file.read_text(encoding="utf-8"))
    assert marker["checkpoint_saved"] is True

    # 5. PostInvocation now terminates cleanly
    post_inv = _run_gate(
        tmp_path,
        "PostInvocation",
        {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro", "invocationNum": 1},
        env,
    )
    assert post_inv.get("terminationBehavior") == "terminate"


def test_multi_window_selects_hard_mode_and_latest_reset(tmp_path):
    early_reset = _future_iso(1200)   # 20 min
    late_reset = _future_iso(172800)  # 2 days
    fixture = _make_usage_fixture(
        tmp_path,
        [
            {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.04, "reset_time": early_reset},
            {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.025, "reset_time": late_reset},
        ],
    )
    res = _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo hi"}},
        },
        {"AGY_USAGE_FIXTURE": str(fixture)},
    )
    assert res["decision"] == "deny"
    marker = json.loads((tmp_path / ".agy-quota" / "STOP.json").read_text(encoding="utf-8"))
    assert marker["mode"] == "hard"
    assert marker["resets_at"] == late_reset
    assert len(marker["triggered_windows"]) == 2


def test_irrelevant_model_group_quota_does_not_stop_active_model(tmp_path):
    fixture = _make_usage_fixture(
        tmp_path,
        [
            {"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.90, "reset_time": _future_iso(1800)},
            {"id": "gemini-weekly", "window": "weekly", "remaining_fraction": 0.90, "reset_time": _future_iso(86400)},
            {"id": "3p-5h", "window": "5h", "remaining_fraction": 0.01, "reset_time": _future_iso(1800)},
            {"id": "3p-weekly", "window": "weekly", "remaining_fraction": 0.01, "reset_time": _future_iso(86400)},
        ],
    )
    res = _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo ok"}},
        },
        {"AGY_USAGE_FIXTURE": str(fixture)},
    )
    assert res == {"decision": "allow"}
    assert not (tmp_path / ".agy-quota" / "STOP.json").exists()


def test_hard_stash_requires_opt_in_clean_checkpoint_and_verified_stash_ref(tmp_path):
    _init_git_repo(tmp_path)
    fixture_ok = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.50, "reset_time": _future_iso(1800)}],
        name="fixture_ok.json",
    )
    _run_gate(
        tmp_path,
        "PostToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {
                "name": "task_boundary",
                "args": {"TaskName": "Clean task", "TaskStatus": "in_progress"},
            },
        },
        {"AGY_USAGE_FIXTURE": str(fixture_ok)},
    )
    (tmp_path / "README.md").write_text("# Modified during task\n", encoding="utf-8")

    fixture_hard = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.02, "reset_time": _future_iso(1800)}],
        name="fixture_hard.json",
    )
    res = _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo test"}},
        },
        {"AGY_USAGE_FIXTURE": str(fixture_hard), "AGY_HARD_ABORT": "1"},
    )
    assert res["decision"] == "deny"
    marker = json.loads((tmp_path / ".agy-quota" / "STOP.json").read_text(encoding="utf-8"))
    assert marker["reverted"] is True
    assert marker["stash_created"] is True
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Test Repo\n"
    state = json.loads((tmp_path / ".agy-quota" / "todos_state.json").read_text(encoding="utf-8"))
    assert state["in_progress"] is None
    assert state["todos"][0]["status"] == "pending"


def test_hard_stash_skips_when_initial_working_tree_was_dirty(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("# Pre-existing uncommitted user work\n", encoding="utf-8")

    fixture_ok = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.50, "reset_time": _future_iso(1800)}],
        name="fixture_ok.json",
    )
    _run_gate(
        tmp_path,
        "PostToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {
                "name": "task_boundary",
                "args": {"TaskName": "Dirty start task", "TaskStatus": "in_progress"},
            },
        },
        {"AGY_USAGE_FIXTURE": str(fixture_ok)},
    )

    fixture_hard = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.01, "reset_time": _future_iso(1800)}],
        name="fixture_hard.json",
    )
    _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo test"}},
        },
        {"AGY_USAGE_FIXTURE": str(fixture_hard), "AGY_HARD_ABORT": "1"},
    )
    marker = json.loads((tmp_path / ".agy-quota" / "STOP.json").read_text(encoding="utf-8"))
    assert marker["reverted"] is False
    assert marker["stash_created"] is False
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Pre-existing uncommitted user work\n"


def test_planted_git_tracked_todos_state_is_ignored_on_hard_stop(tmp_path):
    _init_git_repo(tmp_path)
    qdir = tmp_path / ".agy-quota"
    qdir.mkdir(parents=True, exist_ok=True)
    state_file = qdir / "todos_state.json"
    state_file.write_text(
        json.dumps({
            "todos": [{"content": "planted", "status": "in_progress"}],
            "in_progress": {"content": "planted", "checkpoint_clean": True},
        }),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "-f", ".agy-quota/todos_state.json"], cwd=str(tmp_path), check=True, capture_output=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-m", "plant state"], cwd=str(tmp_path), check=True, capture_output=True, stdin=subprocess.DEVNULL)

    (tmp_path / "README.md").write_text("# User work\n", encoding="utf-8")
    fixture_hard = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.01, "reset_time": _future_iso(1800)}],
        name="fixture_hard.json",
    )
    _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo test"}},
        },
        {"AGY_USAGE_FIXTURE": str(fixture_hard), "AGY_HARD_ABORT": "1"},
    )
    marker = json.loads((qdir / "STOP.json").read_text(encoding="utf-8"))
    assert marker["reverted"] is False
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# User work\n"


def test_config_ignores_project_hard_abort_and_supports_turkish_locale(tmp_path):
    _init_git_repo(tmp_path)
    qdir = tmp_path / ".agy-quota"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "config.json").write_text(
        json.dumps({"hard_abort_enabled": True, "language": "tr"}),
        encoding="utf-8",
    )
    fixture_hard = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.01, "reset_time": _future_iso(1800)}],
        name="fixture_hard.json",
    )
    res = _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo test"}},
        },
        {"AGY_USAGE_FIXTURE": str(fixture_hard)},
    )
    assert res["decision"] == "deny"
    assert "KOTA SERT DURDURMA" in res["reason"]


def test_post_invocation_logs_missing_checkpoint_on_hard_stop(tmp_path):
    fixture_hard = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.01, "reset_time": _future_iso(1800)}],
    )
    res = _run_gate(
        tmp_path,
        "PostInvocation",
        {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro", "invocationNum": 1},
        {"AGY_USAGE_FIXTURE": str(fixture_hard)},
    )
    assert res["terminationBehavior"] == "terminate"
    marker = json.loads((tmp_path / ".agy-quota" / "STOP.json").read_text(encoding="utf-8"))
    assert marker["missing_checkpoint"] is True
    progress_text = (tmp_path / ".agy-quota" / "progress.md").read_text(encoding="utf-8")
    assert "without model checkpoint" in progress_text


def test_expired_stop_marker_is_cleaned_up(tmp_path):
    qdir = tmp_path / ".agy-quota"
    qdir.mkdir(parents=True)
    (qdir / "STOP.json").write_text(
        json.dumps({"mode": "soft", "pct": 85.0, "resets_at": _past_iso(60)}),
        encoding="utf-8",
    )
    fixture_ok = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.50, "reset_time": _future_iso(1800)}],
    )
    res = _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo hi"}},
        },
        {"AGY_USAGE_FIXTURE": str(fixture_ok)},
    )
    assert res == {"decision": "allow"}
    assert not (qdir / "STOP.json").exists()


def test_soft_threshold_waits_for_active_item_completion_and_hard_stops_mid_item(tmp_path):
    _init_git_repo(tmp_path)
    fixture_50 = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.50, "reset_time": _future_iso(1800)}],
        name="f50.json",
    )
    fixture_85 = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.15, "reset_time": _future_iso(1800)}],
        name="f85.json",
    )
    fixture_99 = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.01, "reset_time": _future_iso(1800)}],
        name="f99.json",
    )

    # 1. Start Item 1 at 50%
    _run_gate(
        tmp_path,
        "PostToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "task_boundary", "args": {"TaskName": "Item 1", "TaskStatus": "in_progress"}},
        },
        {"AGY_USAGE_FIXTURE": str(fixture_50)},
    )
    stop_path = tmp_path / ".agy-quota" / "STOP.json"
    assert not stop_path.exists()

    # 2. Quota rises to 85% (soft) while Item 1 is STILL in_progress:
    # PreInvocation, PreToolUse(run_command), PostToolUse(run_command), and updating the SAME Item 1
    # must NOT trigger soft stop.
    env_85 = {"AGY_USAGE_FIXTURE": str(fixture_85)}
    assert _run_gate(tmp_path, "PreInvocation", {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro"}, env_85) == {}
    assert not stop_path.exists()

    assert _run_gate(
        tmp_path,
        "PreToolUse",
        {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro", "toolCall": {"name": "run_command", "args": {}}},
        env_85,
    ) == {"decision": "allow"}
    assert not stop_path.exists()

    assert _run_gate(
        tmp_path,
        "PostToolUse",
        {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro", "toolCall": {"name": "run_command", "args": {}}},
        env_85,
    ) == {}
    assert not stop_path.exists()

    # Updating the SAME active item (still in_progress) is not a completion boundary
    assert _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "task_boundary", "args": {"TaskName": "Item 1", "TaskStatus": "Still working on Item 1"}},
        },
        env_85,
    ) == {"decision": "allow"}
    assert _run_gate(
        tmp_path,
        "PostToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "task_boundary", "args": {"TaskName": "Item 1", "TaskStatus": "Still working on Item 1"}},
        },
        env_85,
    ) == {}
    assert not stop_path.exists()

    assert _run_gate(tmp_path, "PostInvocation", {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro"}, env_85) == {}
    assert not stop_path.exists()

    # 3. Item 1 finishes (TaskStatus="completed") at 85%:
    # PreToolUse allows completing the item; PostToolUse records completion and writes STOP.json (mode="soft")
    assert _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "task_boundary", "args": {"TaskName": "Item 1", "TaskStatus": "completed"}},
        },
        env_85,
    ) == {"decision": "allow"}
    assert _run_gate(
        tmp_path,
        "PostToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "task_boundary", "args": {"TaskName": "Item 1", "TaskStatus": "completed"}},
        },
        env_85,
    ) == {}
    assert stop_path.exists()
    marker = json.loads(stop_path.read_text(encoding="utf-8"))
    assert marker["mode"] == "soft"

    # Trying to start Item 2 is now denied
    deny_item2 = _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "task_boundary", "args": {"TaskName": "Item 2", "TaskStatus": "in_progress"}},
        },
        env_85,
    )
    assert deny_item2["decision"] == "deny"

    # 4. Mid-item 99% (hard) stops immediately even while Item 2 is in_progress
    stop_path.unlink()
    _run_gate(
        tmp_path,
        "PostToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "task_boundary", "args": {"TaskName": "Item 2", "TaskStatus": "in_progress"}},
        },
        {"AGY_USAGE_FIXTURE": str(fixture_50)},
    )
    pre_hard = _run_gate(
        tmp_path,
        "PreInvocation",
        {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro"},
        {"AGY_USAGE_FIXTURE": str(fixture_99)},
    )
    assert "injectSteps" in pre_hard
    assert "QUOTA HARD STOP" in pre_hard["injectSteps"][0]["userMessage"]
    assert json.loads(stop_path.read_text(encoding="utf-8"))["mode"] == "hard"


def test_hard_stash_works_when_agy_quota_is_in_gitignore(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".agy-quota/\n", encoding="utf-8")
    (tmp_path / "work.txt").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "work.txt"], cwd=str(tmp_path), check=True, capture_output=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "commit", "-m", "add gitignore and work.txt"], cwd=str(tmp_path), check=True, capture_output=True, stdin=subprocess.DEVNULL)

    fixture_ok = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.50, "reset_time": _future_iso(1800)}],
        name="f_ok.json",
    )
    _run_gate(
        tmp_path,
        "PostToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "task_boundary", "args": {"TaskName": "Gitignored task", "TaskStatus": "in_progress"}},
        },
        {"AGY_USAGE_FIXTURE": str(fixture_ok)},
    )

    # Modify tracked file and add untracked file during task
    (tmp_path / "work.txt").write_text("modified\n", encoding="utf-8")
    (tmp_path / "extra.txt").write_text("untracked\n", encoding="utf-8")

    fixture_hard = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.01, "reset_time": _future_iso(1800)}],
        name="f_hard.json",
    )
    res = _run_gate(
        tmp_path,
        "PreToolUse",
        {
            "workspacePaths": [str(tmp_path)],
            "modelName": "gemini-2.5-pro",
            "toolCall": {"name": "run_command", "args": {"CommandLine": "echo x"}},
        },
        {"AGY_USAGE_FIXTURE": str(fixture_hard), "AGY_HARD_ABORT": "1"},
    )
    assert res["decision"] == "deny"
    marker = json.loads((tmp_path / ".agy-quota" / "STOP.json").read_text(encoding="utf-8"))
    assert marker["reverted"] is True
    assert marker["stash_created"] is True
    assert (tmp_path / "work.txt").read_text(encoding="utf-8") == "initial\n"
    assert not (tmp_path / "extra.txt").exists()



def test_soft_marker_escalates_to_hard_and_keeps_checkpoint_saved(tmp_path):
    fixture_soft = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.15, "reset_time": _future_iso(1800)}],
        name="fixture_soft.json",
    )
    env_soft = {"AGY_USAGE_FIXTURE": str(fixture_soft)}
    base = {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro"}
    progress = str(tmp_path / ".agy-quota" / "progress.md")

    _run_gate(tmp_path, "PreInvocation", dict(base, invocationNum=1), env_soft)
    _run_gate(
        tmp_path,
        "PostToolUse",
        dict(base, toolCall={"name": "write_to_file", "args": {"TargetFile": progress}}),
        env_soft,
    )
    stop_file = tmp_path / ".agy-quota" / "STOP.json"
    marker = json.loads(stop_file.read_text(encoding="utf-8"))
    assert marker["mode"] == "soft" and marker["checkpoint_saved"] is True

    fixture_hard = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.01, "reset_time": _future_iso(1800)}],
        name="fixture_hard.json",
    )
    env_hard = {"AGY_USAGE_FIXTURE": str(fixture_hard)}
    res = _run_gate(
        tmp_path,
        "PreToolUse",
        dict(base, toolCall={"name": "run_command", "args": {"CommandLine": "echo hi"}}),
        env_hard,
    )
    assert res["decision"] == "deny"
    assert "QUOTA HARD STOP" in res["reason"]
    marker = json.loads(stop_file.read_text(encoding="utf-8"))
    assert marker["mode"] == "hard"
    assert marker["escalated_from"] == "soft"
    assert marker["checkpoint_saved"] is True
    assert marker["revert_status"] == "no_item"

    post_inv = _run_gate(tmp_path, "PostInvocation", dict(base, invocationNum=1), env_hard)
    assert post_inv.get("terminationBehavior") == "terminate"
    marker = json.loads(stop_file.read_text(encoding="utf-8"))
    assert marker["missing_checkpoint"] is False
    progress_text = Path(progress).read_text(encoding="utf-8") if Path(progress).exists() else ""
    assert "without model checkpoint" not in progress_text and "checkpoint yazmadan" not in progress_text


def test_pre_invocation_escalates_existing_soft_marker(tmp_path):
    fixture_soft = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.15, "reset_time": _future_iso(1800)}],
        name="fixture_soft.json",
    )
    base = {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro", "invocationNum": 1}
    _run_gate(tmp_path, "PreInvocation", base, {"AGY_USAGE_FIXTURE": str(fixture_soft)})
    fixture_hard = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.01, "reset_time": _future_iso(1800)}],
        name="fixture_hard.json",
    )
    out = _run_gate(tmp_path, "PreInvocation", base, {"AGY_USAGE_FIXTURE": str(fixture_hard)})
    assert "QUOTA HARD STOP" in out["injectSteps"][0]["userMessage"]
    marker = json.loads((tmp_path / ".agy-quota" / "STOP.json").read_text(encoding="utf-8"))
    assert marker["mode"] == "hard" and marker["escalated_from"] == "soft"


def test_marker_reason_reflects_skipped_revert_not_disabled(tmp_path):
    _init_git_repo(tmp_path)
    (tmp_path / "README.md").write_text("# dirty before task\n", encoding="utf-8")
    fixture_ok = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.50, "reset_time": _future_iso(1800)}],
        name="fixture_ok.json",
    )
    base = {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro"}
    _run_gate(
        tmp_path,
        "PostToolUse",
        dict(base, toolCall={"name": "task_boundary", "args": {"TaskName": "Dirty", "TaskStatus": "in_progress"}}),
        {"AGY_USAGE_FIXTURE": str(fixture_ok)},
    )
    fixture_hard = _make_usage_fixture(
        tmp_path,
        [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.01, "reset_time": _future_iso(1800)}],
        name="fixture_hard.json",
    )
    env_hard = {"AGY_USAGE_FIXTURE": str(fixture_hard), "AGY_HARD_ABORT": "1"}
    _run_gate(
        tmp_path,
        "PreToolUse",
        dict(base, toolCall={"name": "run_command", "args": {"CommandLine": "echo"}}),
        env_hard,
    )
    marker = json.loads((tmp_path / ".agy-quota" / "STOP.json").read_text(encoding="utf-8"))
    assert marker["revert_status"] == "skipped"
    out = _run_gate(tmp_path, "PreInvocation", dict(base, invocationNum=2), env_hard)
    msg = out["injectSteps"][0]["userMessage"]
    assert "Auto-revert was skipped" in msg
    assert "is disabled (default)" not in msg


def test_soft_stop_sends_localized_status_notification(tmp_path):
    import http.server
    import threading

    received: list[tuple[dict, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            received.append((dict(self.headers), self.rfile.read(length).decode("utf-8")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        fixture = _make_usage_fixture(
            tmp_path,
            [{"id": "gemini-5h", "window": "5h", "remaining_fraction": 0.15, "reset_time": _future_iso(1800)}],
        )
        _run_gate(
            tmp_path,
            "PreInvocation",
            {"workspacePaths": [str(tmp_path)], "modelName": "gemini-2.5-pro", "invocationNum": 1},
            {
                "AGY_USAGE_FIXTURE": str(fixture),
                "AGY_NOTIFY_URL": f"http://127.0.0.1:{server.server_port}/topic",
                "AGY_LANG": "tr",
            },
        )
    finally:
        server.shutdown()
        server.server_close()
    assert len(received) == 1
    headers, body = received[0]
    assert headers.get("Title") == "agy-quota-guard SOFT STOP"
    assert body.startswith("[agy-quota-guard] YUMUŞAK DURDURMA")
    assert str(tmp_path) not in body


def test_notify_rejects_non_http_schemes(monkeypatch):
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import notify
    finally:
        sys.path.remove(str(SCRIPTS_DIR))
    monkeypatch.delenv("AGY_PLUGIN_OPTION_NOTIFY_URL", raising=False)
    monkeypatch.setenv("AGY_NOTIFY_URL", "file:///etc/passwd")
    assert notify.resolve_url() is None
    assert notify.send("x") is False
    monkeypatch.setenv("AGY_NOTIFY_URL", "https://ntfy.sh/topic")
    assert notify.resolve_url() == "https://ntfy.sh/topic"
