import json
import os
import time

import pytest

import run


@pytest.fixture
def harness(tmp_path, monkeypatch, fake_cli):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(run, 'codex_command', lambda: fake_cli)
    monkeypatch.setattr(run, 'get_usage', lambda **kw: [])
    monkeypatch.setattr(run, 'notify', lambda text: None)
    return tmp_path


def args(*extra):
    return run.parser().parse_args(['--resume-buffer', '0', '--retry-backoff', '0', '--max-retries', '1', *extra])


def calls(project):
    return [json.loads(line) for line in (project / '.codex-quota/calls.jsonl').read_text().splitlines()]


def test_stop_resumes_exact_id_and_long_stdin(harness, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'stop-resume')
    prompt = 'ş' * 50000
    assert run.execute(args(prompt)) == 0
    invoked = calls(harness)
    assert len(invoked) == 2
    assert prompt in invoked[0]['prompt']
    assert 'resume' in invoked[1]['argv'] and 'thread-test-123' in invoked[1]['argv']
    assert '--last' not in invoked[1]['argv']


def test_initial_failure_retries_original_task(harness, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'first-fail')
    assert run.execute(args('original task')) == 0
    invoked = calls(harness)
    assert all('resume' not in c['argv'] and 'original task' in c['prompt'] for c in invoked)


def test_no_id_never_falls_back(harness, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'no-id')
    with pytest.raises(RuntimeError):
        run.execute(args('task'))
    assert len(calls(harness)) == 1


def test_logical_failure_exit_zero_is_retried(harness, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'logical-failure')
    assert run.execute(args('task')) == 1
    assert len(calls(harness)) == 2


def test_stale_done_is_not_reused(harness, monkeypatch):
    folder = harness / '.codex-quota'
    folder.mkdir()
    (folder / 'progress.md').write_text('CODEX_QUOTA_DONE\n')
    monkeypatch.setenv('FAKE_MODE', 'unfinished')
    with pytest.raises(RuntimeError, match='without a completion marker'):
        run.execute(args('new task'))
    assert len(list(folder.glob('progress-*.md'))) == 1


def test_manual_resume_uses_saved_thread(harness, monkeypatch):
    assert run.execute(args('task')) == 0
    assert run.execute(args('--resume')) == 0
    assert 'thread-test-123' in calls(harness)[1]['argv']


def test_timeout_kills_descendants(harness, monkeypatch, fake_cli):
    monkeypatch.setenv('FAKE_MODE', 'hang')
    result = run.run_round(fake_cli + ['exec'], 'task', 0.5, lambda x: None, str(harness), dict(os.environ))
    assert result == 124
    time.sleep(2.1)
    assert not (harness / 'child-survived').exists()


def test_lock_prevents_concurrent_runner(harness):
    folder = harness / '.codex-quota'
    folder.mkdir()
    (folder / 'runner.lock').write_text('123')
    with pytest.raises(RuntimeError, match='runner lock'):
        run.execute(args('task'))


def test_preflight_waits_without_spending_turn(harness, monkeypatch):
    values = iter([[dict(pct=99, resets_at=time.time() + 0.05, minutes=300)], []])
    monkeypatch.setattr(run, 'get_usage', lambda **kw: next(values))
    assert run.execute(args('task')) == 0
    assert len(calls(harness)) == 1


def test_fatal_runtime_error_sends_failed_notification(harness, monkeypatch):
    sent = []
    monkeypatch.setattr(run, 'notify', sent.append)
    monkeypatch.setenv('FAKE_MODE', 'no-id')
    monkeypatch.setattr('sys.argv', ['codex-run', '--resume-buffer', '0', '--retry-backoff', '0',
                                     '--max-retries', '1', '--lang', 'tr', 'task'])
    assert run.main() == 1
    assert sent == [run.message('failed', 'tr')]
