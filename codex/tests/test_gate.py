import json
from pathlib import Path
import subprocess
import time

import pytest

from common import config, git, read_json, state_dir, write_json
import quota_gate as gate


def payload(project, plan=None):
    return dict(cwd=str(project), session_id='thread-one', hook_event_name='PostToolUse',
                tool_name='update_plan' if plan is not None else 'Bash',
                tool_input={'plan': plan} if plan is not None else {})


@pytest.fixture
def quota(monkeypatch):
    windows = [dict(pct=85, resets_at=time.time() + 3600, minutes=300, limit_id='codex', window='primary')]
    monkeypatch.setattr(gate, 'get_usage', lambda: windows)
    monkeypatch.setattr(gate, 'notify', lambda text: None)
    return windows


def test_plans_none_soft_and_stop_fields(tmp_path, quota):
    output = gate.handle(payload(tmp_path))
    assert output['decision'] == 'block'
    assert output['hookSpecificOutput']['hookEventName'] == 'PostToolUse'
    assert read_json(state_dir(tmp_path) / 'STOP.json')['mode'] == 'soft'
    assert gate.handle(payload(tmp_path)) == {}  # checkpoint tools remain usable


def test_active_item_waits_for_boundary(tmp_path, quota):
    quota[0]['pct'] = 10
    gate.handle(payload(tmp_path, [dict(step='item one', status='in_progress')]))
    quota[0]['pct'] = 85
    assert gate.handle(payload(tmp_path)) == {}
    assert gate.handle(payload(tmp_path, [dict(step='item one', status='in_progress')])) == {}
    assert gate.handle(payload(tmp_path, [dict(step='item one', status='completed')]))['decision'] == 'block'


def test_hard_mid_item(tmp_path, quota):
    quota[0]['pct'] = 10
    gate.handle(payload(tmp_path, [dict(step='item one', status='in_progress')]))
    quota[0]['pct'] = 99
    assert gate.handle(payload(tmp_path))['decision'] == 'block'
    assert read_json(state_dir(tmp_path) / 'STOP.json')['reverted'] is False


def test_stop_hook_requests_fresh_checkpoint_once(tmp_path, quota):
    folder = state_dir(tmp_path)
    folder.mkdir()
    progress = folder / 'progress.md'
    progress.write_text('old checkpoint')
    gate.handle(payload(tmp_path))
    stop = dict(payload(tmp_path), hook_event_name='Stop')
    assert gate.handle(stop)['decision'] == 'block'
    assert gate.handle(dict(stop, stop_hook_active=True)) == {}
    marker = read_json(folder / 'STOP.json')
    __import__('os').utime(progress, ns=(marker['progress_mtime'] + 1000000000,) * 2)
    assert gate.handle(stop) == {}


def test_project_cannot_enable_stash_or_webhook(tmp_path):
    write_json(state_dir(tmp_path) / 'config.json', dict(hard_abort=True, notify_url='http://example.com', session_soft=0))
    cfg = config(tmp_path)
    assert not cfg['hard_abort'] and cfg['session_soft'] == 80


def repository(path):
    for args in [('init',), ('config', 'user.email', 'test@example.invalid'), ('config', 'user.name', 'Test')]:
        assert git(path, *args)[0] == 0
    (path / 'file.txt').write_text('original')
    (path / '.gitignore').write_text('.codex-quota/\n')
    assert git(path, 'add', '.')[0] == 0
    assert git(path, 'commit', '-m', 'fixture')[0] == 0


def test_real_stash_success_preserves_state_and_marks_pending(tmp_path):
    repository(tmp_path)
    state = gate.plan_state(tmp_path, {}, [dict(step='edit', status='in_progress')], 'thread-one')
    write_json(state_dir(tmp_path) / 'todos_state.json', state)
    (tmp_path / 'file.txt').write_text('modified')
    assert gate.revert_item(tmp_path, state)
    assert (tmp_path / 'file.txt').read_text() == 'original'
    assert state['todos'][0]['status'] == 'pending'
    assert (state_dir(tmp_path) / 'todos_state.json').exists()


def test_empty_stash_and_dirty_start_never_claim_reverted(tmp_path):
    repository(tmp_path)
    state = gate.plan_state(tmp_path, {}, [dict(step='edit', status='in_progress')], 'thread-one')
    assert not gate.revert_item(tmp_path, state)
    assert state['in_progress']
    (tmp_path / 'file.txt').write_text('user changes')
    dirty = gate.plan_state(tmp_path, {}, [dict(step='edit', status='in_progress')], 'thread-one')
    assert not gate.revert_item(tmp_path, dirty)
    assert (tmp_path / 'file.txt').read_text() == 'user changes'


def test_failed_stash_does_not_change_task(tmp_path, monkeypatch):
    state = dict(todos=[dict(content='edit', status='in_progress')],
                 in_progress=dict(content='edit', clean=True, commit='abc'))
    monkeypatch.setattr(gate, 'tracked', lambda *args: False)
    def failed_git(p, *args, **kwargs):
        if args == ('rev-parse', 'HEAD'):
            return 0, 'abc'
        if args[0] == 'diff':
            return 0, 'file.txt\0'
        if args[0] == 'ls-files':
            return 0, ''
        return 1, ''
    monkeypatch.setattr(gate, 'git', failed_git)
    assert not gate.revert_item(tmp_path, state)
    assert state['todos'][0]['status'] == 'in_progress'


def test_tracked_planted_state_ignored(tmp_path, quota):
    repository(tmp_path)
    write_json(state_dir(tmp_path) / 'todos_state.json', dict(session_id='thread-one'))
    assert git(tmp_path, 'add', '-f', '.codex-quota/todos_state.json')[0] == 0
    assert gate.handle(payload(tmp_path)) == {}


def test_symlink_directory_refused(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    try:
        (tmp_path / '.codex-quota').symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip('Symlinks unavailable')
    with pytest.raises(ValueError, match='Unsafe'):
        state_dir(tmp_path)


def test_unavailable_quota_hook_exits_zero(tmp_path):
    script = Path(gate.__file__)
    env = dict(__import__('os').environ, CODEX_QUOTA_CLI=str(tmp_path / 'missing'))
    result = subprocess.run([__import__('sys').executable, str(script)], input=json.dumps(payload(tmp_path)),
                            capture_output=True, text=True, env=env)
    assert result.returncode == 0 and json.loads(result.stdout) == {}
