"""
Shared pytest fixtures for cc-quota-guard.

scripts/*.py are standalone scripts (loaded via each other's own
sys.path.insert, not an installable package), so tests import them the
same way: put scripts/ on sys.path once, here, rather than in every test
file.
"""

import os
import shutil
import subprocess
import sys
import time

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)


@pytest.fixture(autouse=True)
def _isolated_from_real_machine_state(tmp_path, monkeypatch):
    """No test may read a real `usage.py --simulate` file from the
    developer's home dir, or inherit a cc-run / chat-mode env var."""
    monkeypatch.setenv("CC_QUOTA_SIM_FILE", str(tmp_path / "no-simulation.json"))
    monkeypatch.setenv("QUOTA_GUARD_CONFIG", str(tmp_path / "no-shared-settings.json"))
    for name in ("CC_QUOTA_RUNNER", "CC_CHAT_AUTO_RESUME", "CLAUDE_PLUGIN_OPTION_CHAT_AUTO_RESUME",
                 "CC_NOTIFY_URL", "CLAUDE_PLUGIN_OPTION_NOTIFY_URL", "CC_RESUME_BUFFER"):
        monkeypatch.delenv(name, raising=False)
    try:
        import usage
        monkeypatch.setattr(usage, "SIM_FILE", str(tmp_path / "no-simulation.json"))
    except Exception:
        pass


def run_subprocess(cmd, **kwargs):
    """
    subprocess.run with retries on a known-flaky Windows quirk:
    subprocess.Popen occasionally fails stdio handle inheritance —
    "OSError: [WinError 6] The handle is invalid" — under many short-lived
    subprocesses in quick succession. It's a CPython/Windows artifact
    unrelated to the command being run; every subprocess call in this test
    suite goes through here rather than calling subprocess.run directly.
    """
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    last_exc = None
    for attempt in range(5):
        try:
            return subprocess.run(cmd, **kwargs)
        except OSError as exc:
            last_exc = exc
            time.sleep(0.05 * (attempt + 1))
    raise last_exc


def git(repo, *args):
    r = run_subprocess(["git", "-C", str(repo), *args])
    return r.returncode, r.stdout.strip(), r.stderr.strip()


@pytest.fixture
def git_repo(tmp_path):
    """A real, throwaway git repo (subprocess git, not a fake) with one
    initial commit, for tests that need is_git=True / _git() to work
    against something real (e.g. _is_git_tracked, hard-abort revert)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert git(repo, "init", "-q")[0] == 0
    assert git(repo, "config", "user.email", "test@example.com")[0] == 0
    assert git(repo, "config", "user.name", "Test")[0] == 0
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    assert git(repo, "add", "base.txt")[0] == 0
    assert git(repo, "commit", "-q", "-m", "init")[0] == 0
    return repo


@pytest.fixture
def has_symlink_support(tmp_path):
    """True if this platform/user can actually create symlinks (Windows
    needs Developer Mode or admin; skip symlink-specific tests otherwise
    rather than failing on an environment limitation)."""
    target = tmp_path / "symlink_probe_target"
    target.mkdir()
    link = tmp_path / "symlink_probe_link"
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False
    finally:
        if link.exists() or link.is_symlink():
            try:
                link.unlink()
            except OSError:
                pass
        shutil.rmtree(target, ignore_errors=True)
