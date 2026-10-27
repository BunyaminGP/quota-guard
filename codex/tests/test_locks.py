"""Lock files left behind or planted in a cloned repo must not switch the guard off."""
import os
import subprocess
import time

import pytest

from common import state_dir
import quota_gate as gate


def _git_repo(path):
    for cmd in (['git', 'init', '-q'], ['git', 'config', 'user.email', 't@e.st'], ['git', 'config', 'user.name', 't']):
        subprocess.run(cmd, cwd=path, check=True, stdin=subprocess.DEVNULL, capture_output=True)


def test_stale_hook_lock_is_cleared(tmp_path):
    folder = state_dir(tmp_path)
    folder.mkdir()
    lock = folder / 'hook.lock'
    lock.write_text('', encoding='utf-8')
    assert gate.take_hook_lock(tmp_path, folder) is None          # fresh: another hook owns it
    old = time.time() - gate.HOOK_LOCK_STALE - 5
    os.utime(lock, (old, old))
    assert gate.take_hook_lock(tmp_path, folder) == lock          # stale: taken over


def test_committed_hook_lock_is_ignored(tmp_path):
    _git_repo(tmp_path)
    folder = state_dir(tmp_path)
    folder.mkdir()
    (folder / 'hook.lock').write_text('', encoding='utf-8')
    subprocess.run(['git', 'add', '-f', '.codex-quota/hook.lock'], cwd=tmp_path, check=True, stdin=subprocess.DEVNULL, capture_output=True)
    assert gate.take_hook_lock(tmp_path, folder) is not None


def test_runner_lock_counts_only_with_a_live_untracked_pid(tmp_path, monkeypatch):
    _git_repo(tmp_path)
    folder = state_dir(tmp_path)
    folder.mkdir()
    lock = folder / 'runner.lock'
    lock.write_text(str(os.getpid()), encoding='utf-8')
    monkeypatch.setattr(gate.resume_core, 'pid_alive', lambda pid: pid == os.getpid())
    assert gate.runner_active(tmp_path, folder) is True
    lock.write_text('999999', encoding='utf-8')                   # dead runner
    assert gate.runner_active(tmp_path, folder) is False
    lock.write_text(str(os.getpid()), encoding='utf-8')
    subprocess.run(['git', 'add', '-f', '.codex-quota/runner.lock'], cwd=tmp_path, check=True, stdin=subprocess.DEVNULL, capture_output=True)
    assert gate.runner_active(tmp_path, folder) is False          # came with the repo
