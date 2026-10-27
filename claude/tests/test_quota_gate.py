"""
Tests for scripts/quota_gate.py — the PostToolUse hook. Covers the
security-hardening and correctness fixes made across 0.4.0-0.6.0: the
`.cc-quota` symlink/non-directory guard, threshold clamping, planted/
tracked-state distrust for hard-abort, TodoWrite vs TaskCreate/TaskUpdate/
TaskList state tracking, and config precedence.

These mirror scenarios that were originally verified by hand with real
subprocess runs during development — see CHANGELOG.md 0.4.0/0.5.0 entries
for the incidents that motivated each one.
"""

import json
import os
import sys

import pytest

import quota_gate as qg

from conftest import git


# --------------------------------------------------------------------------- _parse_pct

@pytest.mark.parametrize("value,expected", [
    ("80", 80.0),
    (80, 80.0),
    (100, 100.0),
    (0.5, 0.5),
])
def test_parse_pct_accepts_valid_range(value, expected):
    assert qg._parse_pct(value) == expected


@pytest.mark.parametrize("value", [
    "not a number", "80%", 0, -5, 100.1, 99999, float("nan"), None, "",
])
def test_parse_pct_rejects_invalid_or_out_of_range(value):
    assert qg._parse_pct(value) is None


# --------------------------------------------------------------------------- _parse_lang

def test_parse_lang_accepts_shipped_languages():
    assert qg._parse_lang("en") == "en"
    assert qg._parse_lang("TR") == "tr"


def test_parse_lang_rejects_unknown():
    assert qg._parse_lang("xx") is None
    assert qg._parse_lang("") is None


# --------------------------------------------------------------------------- _quota_dir_is_safe

def test_quota_dir_safe_when_absent(tmp_path):
    assert qg._quota_dir_is_safe(str(tmp_path)) is True


def test_quota_dir_safe_when_real_directory(tmp_path):
    os.makedirs(os.path.join(str(tmp_path), ".cc-quota"))
    assert qg._quota_dir_is_safe(str(tmp_path)) is True


def test_quota_dir_unsafe_when_file_collision(tmp_path):
    """Simulates the Windows case: git checks out a committed symlink as a
    plain file when core.symlinks is off (the default)."""
    with open(os.path.join(str(tmp_path), ".cc-quota"), "w") as f:
        f.write("not a directory")
    assert qg._quota_dir_is_safe(str(tmp_path)) is False


def test_quota_dir_unsafe_when_symlink(tmp_path, has_symlink_support):
    if not has_symlink_support:
        pytest.skip("platform/user can't create symlinks here")
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.symlink(str(target), os.path.join(str(tmp_path), ".cc-quota"), target_is_directory=True)
    assert qg._quota_dir_is_safe(str(tmp_path)) is False


def test_writers_refuse_silently_on_unsafe_quota_dir(tmp_path):
    proj = str(tmp_path)
    with open(os.path.join(proj, ".cc-quota"), "w") as f:
        f.write("not a directory")
    # None of these may raise, and none may create anything under the
    # colliding path.
    qg._write_todos_state(proj, {"todos": [], "in_progress": None})
    qg._write_marker(proj, {"mode": "soft"})
    qg._append_progress_note(proj, "note")
    assert qg._read_todos_state(proj) is None


def test_writers_work_normally_on_safe_quota_dir(tmp_path):
    proj = str(tmp_path)
    qg._write_todos_state(proj, {"todos": [], "in_progress": None})
    assert os.path.exists(os.path.join(proj, ".cc-quota", "todos_state.json"))
    state = qg._read_todos_state(proj)
    assert state == {"todos": [], "in_progress": None}


# --------------------------------------------------------------------------- _save_todos_state (TodoWrite)

def test_save_todos_state_tracks_in_progress(tmp_path):
    proj = str(tmp_path)
    todos = [{"content": "item one", "status": "in_progress"}]
    state = qg._save_todos_state(proj, todos, is_git=False)
    assert state["in_progress"]["content"] == "item one"


def test_save_todos_state_keeps_checkpoint_when_same_item_continues(tmp_path):
    proj = str(tmp_path)
    todos = [{"content": "item one", "status": "in_progress"}]
    first = qg._save_todos_state(proj, todos, is_git=False)
    second = qg._save_todos_state(proj, todos, is_git=False)
    assert second["in_progress"]["started_at"] == first["in_progress"]["started_at"]


def test_save_todos_state_clears_in_progress_when_none_active(tmp_path):
    proj = str(tmp_path)
    qg._save_todos_state(proj, [{"content": "x", "status": "in_progress"}], is_git=False)
    state = qg._save_todos_state(proj, [{"content": "x", "status": "completed"}], is_git=False)
    assert state["in_progress"] is None


# --------------------------------------------------------------------------- _save_task_tool_state (TaskCreate/TaskUpdate/TaskList)

def test_task_tool_state_incremental_tracking(tmp_path):
    proj = str(tmp_path)
    qg._save_task_tool_state(
        proj, "TaskCreate", {"subject": "Task A"}, {"task": {"id": "1", "subject": "Task A"}}, is_git=False,
    )
    qg._save_task_tool_state(
        proj, "TaskCreate", {"subject": "Task B"}, {"task": {"id": "2", "subject": "Task B"}}, is_git=False,
    )
    state = qg._save_task_tool_state(
        proj, "TaskUpdate", {"taskId": "1", "status": "in_progress"},
        {"taskId": "1", "statusChange": {"from": "pending", "to": "in_progress"}}, is_git=False,
    )
    assert state["in_progress"]["content"] == "Task A"
    assert {"id": "1", "content": "Task A", "status": "in_progress"} in state["todos"]
    assert {"id": "2", "content": "Task B", "status": "pending"} in state["todos"]

    state = qg._save_task_tool_state(
        proj, "TaskUpdate", {"taskId": "1", "status": "completed"},
        {"taskId": "1", "statusChange": {"from": "in_progress", "to": "completed"}}, is_git=False,
    )
    assert state["in_progress"] is None


def test_task_tool_state_taskcreate_missing_subject_falls_back(tmp_path):
    """Regression test: a TaskCreate whose tool_response/tool_input both omit
    'subject' must not store content=None — that value later feeds
    _do_hard_stop's aborted_item[:60] slice (via a subsequent TaskUpdate to
    in_progress) and a None there crashes the hook. Falls back to a
    placeholder instead, mirroring the TaskUpdate branch's own fallback."""
    proj = str(tmp_path)
    state = qg._save_task_tool_state(
        proj, "TaskCreate", {}, {"task": {"id": "1"}}, is_git=False,
    )
    assert state["todos"][0]["content"] is not None
    assert state["todos"][0]["content"] == "task 1"


def test_task_tool_state_tasklist_full_resync(tmp_path):
    proj = str(tmp_path)
    state = qg._save_task_tool_state(
        proj, "TaskList", {},
        {"tasks": [
            {"id": "1", "subject": "A", "status": "completed"},
            {"id": "2", "subject": "B", "status": "in_progress"},
        ]},
        is_git=False,
    )
    assert state["in_progress"]["content"] == "B"


def test_todowrite_and_tasktools_produce_the_same_state_shape(tmp_path):
    """The SOFT-threshold / hard-abort code downstream only cares about
    state["in_progress"]["content"] and state["todos"][*]["content"] — both
    tool families must populate those the same way."""
    a = qg._save_todos_state(str(tmp_path / "a"), [{"content": "x", "status": "in_progress"}], is_git=False)
    b = qg._save_task_tool_state(
        str(tmp_path / "b"), "TaskUpdate", {"taskId": "1", "status": "in_progress"},
        {"taskId": "1", "statusChange": {"from": "pending", "to": "in_progress"}}, is_git=False,
    )
    assert set(a["in_progress"].keys()) == set(b["in_progress"].keys())


# --------------------------------------------------------------------------- _is_git_tracked / _sanitize_state_for_revert

def test_sanitize_trusts_untracked_state(git_repo):
    proj = str(git_repo)
    state = {"in_progress": {"content": "real work"}, "todos": []}
    sanitized = qg._sanitize_state_for_revert(proj, state, is_git=True)
    assert sanitized["in_progress"] is not None


def test_sanitize_distrusts_git_tracked_state(git_repo):
    """The core regression test for the 0.4.0 security fix: a
    todos_state.json committed into the repo (the opposite of what the
    README tells users to do) must not be able to claim an in_progress
    item for hard-abort revert purposes."""
    proj = str(git_repo)
    os.makedirs(os.path.join(proj, ".cc-quota"))
    state_path = os.path.join(proj, ".cc-quota", "todos_state.json")
    with open(state_path, "w") as f:
        json.dump({"in_progress": {"content": "planted item"}, "todos": []}, f)
    git(proj, "add", ".cc-quota/todos_state.json")
    git(proj, "commit", "-q", "-m", "attacker plants tracked state")

    state = {"in_progress": {"content": "planted item"}, "todos": []}
    sanitized = qg._sanitize_state_for_revert(proj, state, is_git=True)
    assert sanitized["in_progress"] is None


def test_sanitize_noop_when_not_git_or_no_in_progress(tmp_path):
    assert qg._sanitize_state_for_revert(str(tmp_path), None, is_git=True) is None
    state = {"in_progress": None, "todos": []}
    assert qg._sanitize_state_for_revert(str(tmp_path), state, is_git=True) == state
    state = {"in_progress": {"content": "x"}, "todos": []}
    assert qg._sanitize_state_for_revert(str(tmp_path), state, is_git=False) == state


# --------------------------------------------------------------------------- _load_config precedence

def test_load_config_defaults(tmp_path):
    cfg = qg._load_config(str(tmp_path))
    assert cfg["session_soft"] == 80.0
    assert cfg["session_hard"] == 95.0
    assert cfg["weekly_soft"] == 97.0
    assert cfg["weekly_hard"] == 98.0
    assert cfg["hard_abort_enabled"] is False
    assert cfg["language"] == "en"


def test_load_config_reads_config_json(tmp_path):
    os.makedirs(os.path.join(str(tmp_path), ".cc-quota"))
    with open(os.path.join(str(tmp_path), ".cc-quota", "config.json"), "w") as f:
        json.dump({"session_soft": 50, "language": "tr"}, f)
    cfg = qg._load_config(str(tmp_path))
    assert cfg["session_soft"] == 50.0
    assert cfg["language"] == "tr"


def test_load_config_out_of_range_config_json_is_ignored(tmp_path):
    """Regression test: a cloned repo's config.json must not be able to
    neuter the guard (session_hard: 99999) or grief it (session_soft: 0)."""
    os.makedirs(os.path.join(str(tmp_path), ".cc-quota"))
    with open(os.path.join(str(tmp_path), ".cc-quota", "config.json"), "w") as f:
        json.dump({"session_hard": 99999, "session_soft": 0, "weekly_soft": 0, "weekly_hard": -5}, f)
    cfg = qg._load_config(str(tmp_path))
    assert cfg["session_hard"] == 95.0
    assert cfg["session_soft"] == 80.0
    assert cfg["weekly_soft"] == 97.0
    assert cfg["weekly_hard"] == 98.0


def test_load_config_weekly_soft_reads_config_json_and_env(tmp_path, monkeypatch):
    os.makedirs(os.path.join(str(tmp_path), ".cc-quota"))
    with open(os.path.join(str(tmp_path), ".cc-quota", "config.json"), "w") as f:
        json.dump({"weekly_soft": 90}, f)
    cfg = qg._load_config(str(tmp_path))
    assert cfg["weekly_soft"] == 90.0

    monkeypatch.setenv("CC_WEEKLY_SOFT", "85")
    cfg = qg._load_config(str(tmp_path))
    assert cfg["weekly_soft"] == 85.0  # env var wins over config.json


def test_load_config_hard_abort_enabled_never_read_from_config_json(tmp_path):
    """The one setting config.json is deliberately never allowed to
    control — see README's Safety section."""
    os.makedirs(os.path.join(str(tmp_path), ".cc-quota"))
    with open(os.path.join(str(tmp_path), ".cc-quota", "config.json"), "w") as f:
        json.dump({"hard_abort_enabled": True}, f)
    cfg = qg._load_config(str(tmp_path))
    assert cfg["hard_abort_enabled"] is False


def test_load_config_env_var_overrides_config_json(tmp_path, monkeypatch):
    os.makedirs(os.path.join(str(tmp_path), ".cc-quota"))
    with open(os.path.join(str(tmp_path), ".cc-quota", "config.json"), "w") as f:
        json.dump({"session_soft": 50}, f)
    monkeypatch.setenv("CC_SESSION_SOFT", "70")
    cfg = qg._load_config(str(tmp_path))
    assert cfg["session_soft"] == 70.0


def test_load_config_malformed_env_var_does_not_crash(tmp_path, monkeypatch):
    """Regression test: previously only two of three sources wrapped
    float() in a try/except, so a typoed CC_SESSION_SOFT crashed the whole
    hook (breaking fail-open) instead of being ignored."""
    monkeypatch.setenv("CC_SESSION_SOFT", "80%")
    cfg = qg._load_config(str(tmp_path))
    assert cfg["session_soft"] == 80.0  # falls back to default, doesn't raise


# --------------------------------------------------------------------------- _block / notify wiring

def test_block_pushes_notification_with_user_msg(monkeypatch):
    captured = {}

    class FakeNotify:
        @staticmethod
        def notify(message, title=None, url=None):
            captured["message"] = message
            captured["title"] = title
            return True

    monkeypatch.setattr(qg, "notify", FakeNotify)
    with pytest.raises(SystemExit):
        qg._block("some reason for Claude", "some status line for the human")

    assert captured["message"] == "some status line for the human"
    assert captured["title"] == "cc-quota-guard"


def test_block_does_not_crash_when_notify_module_missing(monkeypatch):
    """Fail-open: a missing/broken notify.py (e.g. a partial install) must
    never take the whole hook down with it."""
    monkeypatch.setattr(qg, "notify", None)
    with pytest.raises(SystemExit):
        qg._block("reason", "user msg")


# --------------------------------------------------------------------------- _do_soft_stop

def test_do_soft_stop_session_window(tmp_path, monkeypatch):
    proj = str(tmp_path)
    captured = {}
    monkeypatch.setattr(qg, "_block", lambda reason, user_msg: captured.update(reason=reason, user_msg=user_msg))

    qg._do_soft_stop(proj, "session", 82.0, "2026-01-01T00:00:00Z", 80.0, lang="en")

    assert "82%" in captured["reason"]
    assert "80%" in captured["reason"]
    assert "5-hour session" in captured["reason"]
    marker = json.load(open(os.path.join(proj, ".cc-quota", "STOP.json")))
    assert marker["mode"] == "soft" and marker["window"] == "session"


def test_do_soft_stop_weekly_window_uses_weekly_label(tmp_path, monkeypatch):
    """weekly_soft (default 97) reuses _do_soft_stop with which="week_all" —
    same function as the session case, just a different window/threshold;
    this checks the two don't get their labels/thresholds crossed."""
    proj = str(tmp_path)
    captured = {}
    monkeypatch.setattr(qg, "_block", lambda reason, user_msg: captured.update(reason=reason, user_msg=user_msg))

    qg._do_soft_stop(proj, "week_all", 97.5, "2026-01-01T00:00:00Z", 97.0, lang="en")

    assert "97%" in captured["reason"]
    assert "weekly" in captured["reason"]
    assert "5-hour session" not in captured["reason"]
    marker = json.load(open(os.path.join(proj, ".cc-quota", "STOP.json")))
    assert marker["mode"] == "soft" and marker["window"] == "week_all"


# --------------------------------------------------------------------------- localized user-facing messages

def test_label_and_messages_localized_but_reason_language_is_separate():
    assert qg._label("session", "en") != qg._label("session", "tr")
    en_msg = qg._soft_user_msg("en", qg._label("session", "en"), 90, "2026-01-01T00:00:00Z")
    tr_msg = qg._soft_user_msg("tr", qg._label("session", "tr"), 90, "2026-01-01T00:00:00Z")
    assert "Quota threshold reached" in en_msg
    assert "Kota eşiği" in tr_msg


def test_label_week_sonnet_reuses_weekly_translation_with_model_suffix():
    """"Sonnet" is a model name, not translatable prose, so it's appended
    directly rather than needing its own locale key per language — this
    keeps week_sonnet's label from drifting out of sync with week_all's."""
    assert qg._label("week_sonnet", "en") == qg._label("week_all", "en") + " (Sonnet)"
    assert qg._label("week_sonnet", "tr") == qg._label("week_all", "tr") + " (Sonnet)"


def test_hard_user_msg_distinguishes_reverted_vs_clean():
    reverted = qg._hard_user_msg("en", "session", 96, "2026-01-01T00:00:00Z", did_revert=True)
    clean = qg._hard_user_msg("en", "session", 96, "2026-01-01T00:00:00Z", did_revert=False)
    assert "reverted" in reverted
    assert "clean stop" in clean


# --------------------------------------------------------------------------- end-to-end: hard-abort revert honors sanitize

def test_do_hard_stop_reverts_untracked_in_progress_item(git_repo, monkeypatch):
    proj = str(git_repo)
    (git_repo / ".gitignore").write_text(".cc-quota/\n", encoding="utf-8")
    git(proj, "add", ".gitignore")
    git(proj, "commit", "-q", "-m", "gitignore")
    (git_repo / "work.txt").write_text("uncommitted work\n", encoding="utf-8")

    state = {"in_progress": {"content": "real task", "checkpoint_commit": None}, "todos": [{"content": "real task", "status": "in_progress"}]}

    captured = {}
    monkeypatch.setattr(qg, "_block", lambda reason, user_msg: captured.update(reason=reason, user_msg=user_msg))

    qg._do_hard_stop(proj, "session", 96.0, "2026-01-01T00:00:00Z", 95.0, state, is_git=True, hard_abort_enabled=True, lang="en")

    assert not os.path.exists(os.path.join(proj, "work.txt")), "uncommitted work should have been stashed"
    code, out, _ = git(proj, "stash", "list")
    assert code == 0 and out.strip() != ""
    assert "REVERTED" in captured["reason"]


def test_do_hard_stop_does_not_revert_when_disabled(git_repo, monkeypatch):
    proj = str(git_repo)
    (git_repo / "work.txt").write_text("uncommitted work\n", encoding="utf-8")
    state = {"in_progress": {"content": "real task", "checkpoint_commit": None}, "todos": []}

    captured = {}
    monkeypatch.setattr(qg, "_block", lambda reason, user_msg: captured.update(reason=reason, user_msg=user_msg))

    qg._do_hard_stop(proj, "session", 96.0, "2026-01-01T00:00:00Z", 95.0, state, is_git=True, hard_abort_enabled=False, lang="en")

    assert os.path.exists(os.path.join(proj, "work.txt")), "must not touch files when hard_abort_enabled is False"
    assert "REVERTED" not in captured["reason"]


def test_do_hard_stop_does_not_crash_when_in_progress_content_is_none(git_repo, monkeypatch):
    """Regression test for a real crash found by manual review + repro: an
    in_progress item recorded with content=None (reachable via a TaskCreate
    whose subject was missing, before the _save_task_tool_state fallback
    above existed) used to raise TypeError on aborted_item[:60], crashing
    the hook mid hard-abort instead of failing open."""
    proj = str(git_repo)
    state = {"in_progress": {"content": None, "checkpoint_commit": None}, "todos": []}

    captured = {}
    monkeypatch.setattr(qg, "_block", lambda reason, user_msg: captured.update(reason=reason, user_msg=user_msg))

    qg._do_hard_stop(proj, "session", 96.0, "2026-01-01T00:00:00Z", 95.0, state, is_git=True, hard_abort_enabled=True, lang="en")

    assert "REVERTED" in captured["reason"]
    assert "None" not in captured["reason"]
