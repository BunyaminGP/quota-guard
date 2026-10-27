from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import usage  # noqa: E402


SAMPLE_AGY_USAGE_JSON = {
    "command": {
        "name": "usage",
        "data": {
            "description": "Within each group, models share a weekly limit and a 5-hour limit.",
            "groups": [
                {
                    "name": "Gemini Models",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.80,
                            "reset_time": "2026-10-24T13:44:38Z",
                        },
                        {
                            "id": "gemini-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.25,
                            "reset_time": "2026-10-18T20:20:35Z",
                        },
                    ],
                },
                {
                    "name": "Claude and GPT models",
                    "buckets": [
                        {
                            "id": "3p-weekly",
                            "name": "Weekly Limit Remaining",
                            "window": "weekly",
                            "remaining_fraction": 0.01,
                            "reset_time": "2026-10-25T15:43:20Z",
                        },
                        {
                            "id": "3p-5h",
                            "name": "Five Hour Limit Remaining",
                            "window": "5h",
                            "remaining_fraction": 0.02,
                            "reset_time": "2026-10-18T20:43:20Z",
                        },
                    ],
                },
                {
                    "name": "Gemini 4 Argon",
                    "buckets": [
                        {
                            "id": "gemini-argon-wtu-weekly",
                            "name": "Gemini 4 Argon Weekly Limit",
                            "window": "weekly",
                            "remaining_fraction": 0.70,
                            "reset_time": "2026-10-24T13:19:09Z",
                        }
                    ],
                },
            ],
        },
    }
}


def test_normalize_preserves_all_windows_and_converts_remaining_fraction():
    norm = usage._normalize(SAMPLE_AGY_USAGE_JSON)
    assert norm is not None
    windows = norm["windows"]
    assert len(windows) == 5

    by_id = {w["id"]: w for w in windows}
    assert by_id["gemini-5h"]["pct"] == pytest.approx(75.0)
    assert by_id["gemini-5h"]["kind"] == "session"
    assert by_id["gemini-5h"]["resets_at"] == "2026-10-18T20:20:35Z"

    assert by_id["gemini-weekly"]["pct"] == pytest.approx(20.0)
    assert by_id["gemini-weekly"]["kind"] == "weekly"

    assert by_id["3p-5h"]["pct"] == pytest.approx(98.0)
    assert by_id["3p-weekly"]["pct"] == pytest.approx(99.0)
    assert by_id["gemini-argon-wtu-weekly"]["pct"] == pytest.approx(30.0)


def test_filter_windows_for_model_isolates_relevant_group():
    norm = usage._normalize(SAMPLE_AGY_USAGE_JSON)
    assert norm is not None

    gemini_win = usage.filter_windows_for_model(norm, "gemini-2.5-pro")
    assert {w["id"] for w in gemini_win} == {"gemini-5h", "gemini-weekly"}

    claude_win = usage.filter_windows_for_model(norm, "claude-sonnet-4")
    assert {w["id"] for w in claude_win} == {"3p-5h", "3p-weekly"}

    argon_win = usage.filter_windows_for_model(norm, "gemini-4-argon")
    assert {w["id"] for w in argon_win} == {
        "gemini-argon-wtu-weekly",
        "gemini-5h",
        "gemini-weekly",
    }

    auto_win = usage.filter_windows_for_model(norm, "auto")
    assert len(auto_win) == 5

    unknown_win = usage.filter_windows_for_model(norm, "custom-unknown-model")
    assert len(unknown_win) == 5


def test_get_usage_with_fixture_and_cache(tmp_path, monkeypatch):
    cache_file = tmp_path / "cache.json"
    fixture_file = tmp_path / "usage_fixture.json"
    fixture_file.write_text(json.dumps(SAMPLE_AGY_USAGE_JSON), encoding="utf-8")

    monkeypatch.setenv("AGY_QUOTA_CACHE_FILE", str(cache_file))
    monkeypatch.setenv("AGY_USAGE_FIXTURE", str(fixture_file))

    data = usage.get_usage(force_refresh=True)
    assert data is not None
    assert cache_file.exists()
    assert len(data["windows"]) == 5

    # Remove fixture; cached result should still be returned within TTL
    fixture_file.unlink()
    monkeypatch.setenv("AGY_USAGE_FIXTURE", str(tmp_path / "nonexistent.json"))
    cached = usage.get_usage(force_refresh=False)
    assert cached is not None
    assert len(cached["windows"]) == 5


def test_get_usage_fail_open_when_unavailable(tmp_path, monkeypatch):
    cache_file = tmp_path / "empty_cache.json"
    monkeypatch.setenv("AGY_QUOTA_CACHE_FILE", str(cache_file))
    monkeypatch.setenv("AGY_USAGE_FIXTURE", str(tmp_path / "missing.json"))
    assert usage.get_usage(force_refresh=True) is None


def test_sleep_seconds_until():
    future = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat().replace("+00:00", "Z")
    sec = usage.sleep_seconds_until(future, buffer_sec=60)
    assert sec is not None
    assert 175 <= sec <= 185

    past = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
    assert usage.sleep_seconds_until(past, buffer_sec=45) == 45
    assert usage.sleep_seconds_until("not-a-timestamp") is None
    assert usage.sleep_seconds_until(None) is None
