"""
Tests for scripts/i18n.py — the shared message catalog. See its docstring
and README.md's "Adding a language" section for the contract these tests
enforce: unknown language -> English, missing key -> English, everything
fails open (never raises into the caller).
"""

import json
import os
import sys

import i18n

from conftest import run_subprocess


def test_available_languages_includes_shipped_locales():
    langs = i18n.available_languages()
    assert "en" in langs
    assert "tr" in langs


def test_is_known_language():
    assert i18n.is_known_language("en")
    assert i18n.is_known_language("TR")  # case-insensitive
    assert not i18n.is_known_language("xx")
    assert not i18n.is_known_language("")
    assert not i18n.is_known_language(None)


def test_msg_basic_formatting():
    out = i18n.msg("en", "task_complete")
    assert "Task complete" in out
    out = i18n.msg("tr", "task_complete")
    assert "Görev tamamlandı" in out


def test_msg_placeholder_substitution():
    out = i18n.msg("en", "still_working", waited=45)
    assert "45" in out


def test_msg_unknown_language_falls_back_to_english():
    out = i18n.msg("xx", "task_complete")
    assert out == i18n.msg("en", "task_complete")


def test_msg_missing_key_falls_back_to_raw_key():
    out = i18n.msg("en", "this_key_does_not_exist_anywhere")
    assert out == "this_key_does_not_exist_anywhere"


def test_msg_never_raises_on_missing_kwargs():
    # A template expecting {waited} but called without it must degrade,
    # not throw — a translation/formatting problem must never crash the
    # caller (cc-run, or the hook).
    out = i18n.msg("en", "still_working")
    assert isinstance(out, str)


def test_partial_translation_falls_back_per_key(tmp_path, monkeypatch):
    """A locale file translating only SOME keys must still work: the
    translated ones show the translation, the rest fall back to English —
    this is the whole point of allowing incremental community PRs."""
    locales_dir = tmp_path / "locales"
    locales_dir.mkdir()
    (locales_dir / "xx.json").write_text(
        json.dumps({"task_complete": "XX-TRANSLATED"}), encoding="utf-8"
    )
    monkeypatch.setattr(i18n, "_LOCALES_DIR", str(locales_dir))
    monkeypatch.setattr(i18n, "_cache", {})
    assert i18n.msg("xx", "task_complete") == "XX-TRANSLATED"
    # a key xx.json doesn't have falls back to English's real text
    assert i18n.msg("xx", "ended_own") == i18n.msg("en", "ended_own")


def test_cli_prints_formatted_message():
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "i18n.py")
    result = run_subprocess(
        [sys.executable, script, "en", "still_working_item", "waited=45", "item=some task"],
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert "45" in result.stdout
    assert "some task" in result.stdout


def test_cli_unknown_language_does_not_crash():
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "i18n.py")
    result = run_subprocess([sys.executable, script, "de", "task_complete"], encoding="utf-8")
    assert result.returncode == 0
    assert "Task complete" in result.stdout
