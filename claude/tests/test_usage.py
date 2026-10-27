"""
Tests for scripts/usage.py — _read_token()'s full precedence chain:
CLAUDE_CODE_OAUTH_TOKEN env var, then the credentials file, then (macOS
only) the system Keychain fallback. See usage.py's module docstring and
_read_token_from_keychain()'s docstring for what's confirmed (via Claude
Code's own docs + community bug reports) vs. still unverified (this
project has no real Mac to test the Keychain path against — the
subprocess call itself is mocked here).
"""

import json
import os
import subprocess

import pytest

import usage


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)


# --------------------------------------------------------------------------- CLAUDE_CODE_OAUTH_TOKEN (highest priority, every platform)

def test_read_token_prefers_env_var_over_file(tmp_path, monkeypatch):
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "from-file"}}), encoding="utf-8"
    )
    monkeypatch.setattr(usage, "CREDENTIALS_FILE", str(creds_file))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-env")
    assert usage._read_token() == "from-env"


def test_read_token_env_var_works_even_with_no_file_at_all(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "CREDENTIALS_FILE", os.path.join(str(tmp_path), "nope.json"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "from-env")
    assert usage._read_token() == "from-env"


# --------------------------------------------------------------------------- credentials file (Linux/Windows primary, macOS secondary)

def test_read_token_returns_token_when_file_present(tmp_path, monkeypatch):
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "sk-test-123"}}), encoding="utf-8"
    )
    monkeypatch.setattr(usage, "CREDENTIALS_FILE", str(creds_file))
    assert usage._read_token() == "sk-test-123"


def test_read_token_missing_access_token_in_file_hints_at_keychain(tmp_path, monkeypatch):
    """A file that exists but lacks accessToken is a different failure mode
    than a missing file — still worth the generic hint regardless of
    platform, since it's a real (if unlikely) possibility."""
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(json.dumps({"claudeAiOauth": {}}), encoding="utf-8")
    monkeypatch.setattr(usage, "CREDENTIALS_FILE", str(creds_file))
    monkeypatch.setattr(usage.sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="keychain"):
        usage._read_token()


# --------------------------------------------------------------------------- missing file, non-macOS: no Keychain fallback attempted

def test_read_token_missing_file_on_non_macos_does_not_try_keychain(tmp_path, monkeypatch):
    missing = os.path.join(str(tmp_path), "does-not-exist.json")
    monkeypatch.setattr(usage, "CREDENTIALS_FILE", missing)
    monkeypatch.setattr(usage.sys, "platform", "win32")

    def _boom(*a, **k):
        raise AssertionError("must not shell out to `security` on non-macOS")

    monkeypatch.setattr(usage.subprocess, "run", _boom)
    with pytest.raises(RuntimeError, match="credentials file not found"):
        usage._read_token()


# --------------------------------------------------------------------------- missing file, macOS: Keychain fallback

def test_read_token_missing_file_on_macos_falls_back_to_keychain(tmp_path, monkeypatch):
    missing = os.path.join(str(tmp_path), "does-not-exist.json")
    monkeypatch.setattr(usage, "CREDENTIALS_FILE", missing)
    monkeypatch.setattr(usage.sys, "platform", "darwin")

    def _fake_run(cmd, **kwargs):
        assert cmd[:3] == ["security", "find-generic-password", "-s"]
        assert cmd[3] == "Claude Code-credentials"
        assert "-w" in cmd
        return subprocess.CompletedProcess(
            cmd, returncode=0,
            stdout=json.dumps({"claudeAiOauth": {"accessToken": "from-keychain"}}),
            stderr="",
        )

    monkeypatch.setattr(usage.subprocess, "run", _fake_run)
    assert usage._read_token() == "from-keychain"


def test_read_token_keychain_lookup_failure_raises(tmp_path, monkeypatch):
    missing = os.path.join(str(tmp_path), "does-not-exist.json")
    monkeypatch.setattr(usage, "CREDENTIALS_FILE", missing)
    monkeypatch.setattr(usage.sys, "platform", "darwin")

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=44, stdout="", stderr="item not found")

    monkeypatch.setattr(usage.subprocess, "run", _fake_run)
    with pytest.raises(RuntimeError, match="Keychain lookup failed"):
        usage._read_token()


def test_read_token_keychain_entry_missing_access_token_raises(tmp_path, monkeypatch):
    missing = os.path.join(str(tmp_path), "does-not-exist.json")
    monkeypatch.setattr(usage, "CREDENTIALS_FILE", missing)
    monkeypatch.setattr(usage.sys, "platform", "darwin")

    def _fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=json.dumps({"claudeAiOauth": {}}), stderr="")

    monkeypatch.setattr(usage.subprocess, "run", _fake_run)
    with pytest.raises(RuntimeError, match="accessToken not found in macOS Keychain"):
        usage._read_token()


def test_read_token_keychain_never_attempted_when_file_exists(tmp_path, monkeypatch):
    """Even on macOS, a present credentials file wins over the Keychain —
    matches Claude Code's own file-based fallback (e.g. synced for SSH)."""
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "from-file"}}), encoding="utf-8"
    )
    monkeypatch.setattr(usage, "CREDENTIALS_FILE", str(creds_file))
    monkeypatch.setattr(usage.sys, "platform", "darwin")

    def _boom(*a, **k):
        raise AssertionError("must not shell out to `security` when the file already worked")

    monkeypatch.setattr(usage.subprocess, "run", _boom)
    assert usage._read_token() == "from-file"
