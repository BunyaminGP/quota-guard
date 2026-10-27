from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RUNNER_SCRIPT = PLUGIN_ROOT / "scripts" / "agy_runner.py"
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import notify  # noqa: E402


def _make_mock_agy(tmp_path: Path, script_body: str) -> Path:
    py_script = tmp_path / "mock_agy.py"
    py_script.write_text(script_body, encoding="utf-8")
    if os.name == "nt":
        wrapper = tmp_path / "mock_agy.cmd"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{py_script}" %*\r\n', encoding="utf-8")
        return wrapper
    else:
        py_script.chmod(0o755)
        return py_script


def test_runner_stdin_long_task_and_reset_resume_with_exact_conversation_id(tmp_path):
    reset_ts = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    mock_body = f"""#!{sys.executable}
import json, pathlib, sys

args = sys.argv[1:]
stdin_text = sys.stdin.read()
proj = pathlib.Path.cwd()
qdir = proj / ".agy-quota"
qdir.mkdir(parents=True, exist_ok=True)
calls_file = qdir / "mock_calls.json"
calls = json.loads(calls_file.read_text(encoding="utf-8")) if calls_file.exists() else []
calls.append({{"args": args, "stdin_len": len(stdin_text), "stdin_head": stdin_text[:80]}})
calls_file.write_text(json.dumps(calls), encoding="utf-8")

if len(calls) == 1:
    # Round 1: emit conversation_id and write STOP.json
    (qdir / "STOP.json").write_text(json.dumps({{
        "mode": "soft",
        "window": "gemini-5h",
        "window_label": "gemini-5h (session)",
        "pct": 85.0,
        "threshold": 80.0,
        "resets_at": "{reset_ts}",
        "reverted": False,
        "checkpoint_saved": True
    }}), encoding="utf-8")
    print(json.dumps({{"event": "result", "result": {{"conversation_id": "conv-exact-999", "status": "SUCCESS", "num_turns": 2}}}}), flush=True)
    sys.exit(0)
else:
    # Round 2: must receive --conversation conv-exact-999 (never -c or --continue)
    assert "--conversation" in args, f"Expected --conversation in {{args}}"
    idx = args.index("--conversation")
    assert args[idx + 1] == "conv-exact-999"
    assert "-c" not in args and "--continue" not in args
    pf = qdir / "progress.md"
    pf.write_text(pf.read_text(encoding="utf-8") + "\\nAGY_QUOTA_DONE\\n", encoding="utf-8")
    print(json.dumps({{"event": "result", "result": {{"conversation_id": "conv-exact-999", "status": "SUCCESS", "num_turns": 1}}}}), flush=True)
    sys.exit(0)
"""
    mock_bin = _make_mock_agy(tmp_path, mock_body)
    # Pre-seed stale AGY_QUOTA_DONE to verify archiving on startup
    qdir = tmp_path / ".agy-quota"
    qdir.mkdir(parents=True, exist_ok=True)
    (qdir / "progress.md").write_text("# Old Run\nAGY_QUOTA_DONE\n", encoding="utf-8")

    long_task = "Implement module X with 'single quotes' and \"double quotes\"\n" + ("Line of specification\n" * 300)
    env = os.environ.copy()
    env.update({
        "AGY_CLI_BIN": str(mock_bin),
        "AGY_PROJECT_DIR": str(tmp_path),
        "AGY_RESUME_BUFFER": "0",
        "AGY_RETRY_BACKOFF": "1",
    })
    proc = subprocess.run(
        [sys.executable, str(RUNNER_SCRIPT), "--model", "gemini-2.5-pro"],
        input=long_task,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
        env=env,
        timeout=20,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    calls = json.loads((qdir / "mock_calls.json").read_text(encoding="utf-8"))
    assert len(calls) == 2
    assert "--conversation" not in calls[0]["args"]
    assert calls[0]["stdin_len"] > len(long_task)
    assert "--conversation" in calls[1]["args"]
    assert "conv-exact-999" in calls[1]["args"]
    progress_content = (qdir / "progress.md").read_text(encoding="utf-8")
    assert "AGY_QUOTA_DONE_ARCHIVED" in progress_content


def test_runner_refuses_resume_without_conversation_id(tmp_path):
    reset_ts = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    mock_body = f"""#!{sys.executable}
import json, pathlib, sys
qdir = pathlib.Path.cwd() / ".agy-quota"
qdir.mkdir(parents=True, exist_ok=True)
(qdir / "STOP.json").write_text(json.dumps({{
    "mode": "soft",
    "window": "gemini-5h",
    "pct": 85.0,
    "resets_at": "{reset_ts}"
}}), encoding="utf-8")
# Intentionally emit empty conversation_id
print(json.dumps({{"event": "result", "result": {{"conversation_id": "", "status": "SUCCESS"}}}}), flush=True)
sys.exit(0)
"""
    mock_bin = _make_mock_agy(tmp_path, mock_body)
    env = os.environ.copy()
    env.update({
        "AGY_CLI_BIN": str(mock_bin),
        "AGY_PROJECT_DIR": str(tmp_path),
        "AGY_RESUME_BUFFER": "0",
    })
    proc = subprocess.run(
        [sys.executable, str(RUNNER_SCRIPT), "Test task"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
        env=env,
        timeout=15,
    )
    assert proc.returncode == 3
    assert "conversation ID" in proc.stderr


def test_runner_retries_with_original_task_when_initial_launch_fails_before_id(tmp_path):
    mock_body = f"""#!{sys.executable}
import json, pathlib, sys
stdin_text = sys.stdin.read()
qdir = pathlib.Path.cwd() / ".agy-quota"
qdir.mkdir(parents=True, exist_ok=True)
calls_file = qdir / "calls.json"
calls = json.loads(calls_file.read_text(encoding="utf-8")) if calls_file.exists() else []
calls.append({{"args": sys.argv[1:], "stdin": stdin_text}})
calls_file.write_text(json.dumps(calls), encoding="utf-8")

if len(calls) == 1:
    # Transient startup failure before any conversation ID is produced
    sys.exit(1)

assert "Original unique task prompt" in stdin_text
pf = qdir / "progress.md"
pf.write_text(pf.read_text(encoding="utf-8") + "\\nAGY_QUOTA_DONE\\n", encoding="utf-8")
print(json.dumps({{"event": "result", "result": {{"conversation_id": "conv-after-retry", "status": "SUCCESS"}}}}), flush=True)
sys.exit(0)
"""
    mock_bin = _make_mock_agy(tmp_path, mock_body)
    env = os.environ.copy()
    env.update({
        "AGY_CLI_BIN": str(mock_bin),
        "AGY_PROJECT_DIR": str(tmp_path),
        "AGY_RETRY_BACKOFF": "0",
        "AGY_MAX_RETRIES": "2",
    })
    proc = subprocess.run(
        [sys.executable, str(RUNNER_SCRIPT), "Original unique task prompt"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0
    calls = json.loads((tmp_path / ".agy-quota" / "calls.json").read_text(encoding="utf-8"))
    assert len(calls) == 2
    assert "Original unique task prompt" in calls[1]["stdin"]


def test_runner_timeout_kills_process_tree(tmp_path):
    mock_body = f"""#!{sys.executable}
import time
time.sleep(30)
"""
    mock_bin = _make_mock_agy(tmp_path, mock_body)
    env = os.environ.copy()
    env.update({
        "AGY_CLI_BIN": str(mock_bin),
        "AGY_PROJECT_DIR": str(tmp_path),
        "AGY_ROUND_TIMEOUT": "1",
        "AGY_MAX_RETRIES": "0",
        "AGY_RETRY_BACKOFF": "0",
    })
    proc = subprocess.run(
        [sys.executable, str(RUNNER_SCRIPT), "Hanging task"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(tmp_path),
        env=env,
        timeout=10,
    )
    assert proc.returncode == 124
    assert "timed out" in proc.stderr


def test_notify_sends_plain_text_status_and_ignores_project_config(tmp_path, monkeypatch):
    received: list[dict] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            received.append({"body": body, "title": self.headers.get("Title")})
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_port
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # 1. Without env var, notify does nothing
        monkeypatch.delenv("AGY_NOTIFY_URL", raising=False)
        monkeypatch.delenv("AGY_PLUGIN_OPTION_NOTIFY_URL", raising=False)
        assert notify.send("Status check") is False
        assert len(received) == 0

        # 2. With AGY_NOTIFY_URL, sends status message with [agy-quota-guard] prefix
        monkeypatch.setenv("AGY_NOTIFY_URL", f"http://127.0.0.1:{port}/topic")
        assert notify.send("SOFT STOP: gemini-5h 82.0%", title="agy-quota-guard SOFT STOP") is True
        assert len(received) == 1
        assert received[0]["body"] == "[agy-quota-guard] SOFT STOP: gemini-5h 82.0%"
        assert received[0]["title"] == "agy-quota-guard SOFT STOP"
    finally:
        server.shutdown()


def test_agy_plugin_validate_passes_strict():
    agy_path = shutil.which("agy")
    if not agy_path:
        pytest.skip("agy binary not in PATH")
    proc = subprocess.run(
        [agy_path, "plugin", "validate", str(PLUGIN_ROOT.parent), "--strict", "--json"],
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"
    data = json.loads(proc.stdout)
    assert data["name"] == "quota-guard"
    assert data["components"]["hooks"] == 1
    assert data["findings"] == []


def test_installed_windows_launcher_help_and_nonzero_exit_code(tmp_path):
    plugin_dir = tmp_path / "plugin"
    bin_dir = tmp_path / "bin"
    env = os.environ.copy()
    env["AGY_PLUGIN_INSTALL_DIR"] = str(plugin_dir)
    env["AGY_BIN_INSTALL_DIR"] = str(bin_dir)

    if os.name == "nt":
        install_proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PLUGIN_ROOT / "install.ps1")],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=20,
        )
        assert install_proc.returncode == 0, f"stderr={install_proc.stderr}"
        shim = bin_dir / "agy-run.cmd"
        assert shim.exists()

        help_proc = subprocess.run(
            ["cmd", "/c", str(shim), "--help"],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(tmp_path),
            timeout=15,
        )
        assert help_proc.returncode == 0, f"stderr={help_proc.stderr}"
        assert "Usage: agy-run" in help_proc.stdout

        fail_proc = subprocess.run(
            ["cmd", "/c", str(shim), "   "],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(tmp_path),
            timeout=15,
        )
        assert fail_proc.returncode == 2
    else:
        install_proc = subprocess.run(
            ["bash", str(PLUGIN_ROOT / "install.sh")],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=20,
        )
        assert install_proc.returncode == 0
        shim = bin_dir / "agy-run"
        help_proc = subprocess.run(
            [str(shim), "--help"],
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(tmp_path),
            timeout=15,
        )
        assert help_proc.returncode == 0
        assert "Usage: agy-run" in help_proc.stdout



def test_runner_refuses_a_planted_progress_symlink(tmp_path):
    qdir = tmp_path / ".agy-quota"
    qdir.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    try:
        os.symlink(victim, qdir / "progress.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    env = os.environ.copy()
    env.update({"AGY_CLI_BIN": sys.executable, "AGY_PROJECT_DIR": str(tmp_path)})
    proc = subprocess.run([sys.executable, str(RUNNER_SCRIPT), "task"], stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", cwd=str(tmp_path), env=env, timeout=15)
    assert proc.returncode == 2 and "Refusing to run" in proc.stderr
    assert victim.read_text(encoding="utf-8") == "keep me"
