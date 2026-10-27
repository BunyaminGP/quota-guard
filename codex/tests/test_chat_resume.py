"""Chat auto-resume for normal interactive Codex chats (no codex-run).

stop -> background watcher planned -> quota re-checked at the planned time ->
"quota reset" ping + `codex queue` -> first tool call "working again" ->
end of turn "done".
"""
import json
import time

import pytest

from common import read_json, state_dir, write_json
import quota_gate as gate
import resume_watcher as watcher

SESSION = 'thread-chat-1'


def payload(project, event='PostToolUse', **extra):
    value = dict(cwd=str(project), session_id=SESSION, hook_event_name=event, tool_name='Bash', tool_input={})
    value.update(extra)
    return value


@pytest.fixture
def pings(monkeypatch):
    sent = []
    monkeypatch.setattr(gate, 'notify', lambda text, title='codex-quota-guard': sent.append((title, text)))
    return sent


@pytest.fixture
def high(monkeypatch):
    windows = [dict(pct=96, resets_at=time.time() + 3600, minutes=300, limit_id='codex', window='primary')]
    monkeypatch.setattr(gate, 'get_usage', lambda: windows)
    monkeypatch.setenv('CODEX_QUOTA_LANG', 'tr')
    return windows


def test_chat_stop_plans_a_wake_up_and_says_when(tmp_path, pings, high, monkeypatch):
    calls = []
    monkeypatch.setattr(gate.resume_core, 'spawn_detached', lambda argv, **kw: calls.append(argv) or 777)
    out = gate.handle(payload(tmp_path))
    assert out['decision'] == 'block' and 'AUTO-RESUME' in out['reason']
    assert 'Otomatik devam' in out['systemMessage']
    marker = read_json(state_dir(tmp_path) / 'STOP.json')
    assert marker['chat'] is True and marker['resume_at'] >= high[0]['resets_at']
    plan = read_json(state_dir(tmp_path) / 'watcher.json')
    assert plan['session_id'] == SESSION and plan['pid'] == 777 and plan['fire_at'] == marker['resume_at']
    assert calls and calls[0][1].endswith('resume_watcher.py') and plan['token'] in calls[0]
    assert pings == [('codex-quota-guard', out['systemMessage'])]


@pytest.mark.parametrize('env', [{'CODEX_QUOTA_CHAT_AUTO_RESUME': '0'}, {'CODEX_QUOTA_RUN_ID': 'run-1'}])
def test_runner_or_disabled_keeps_plain_stop(tmp_path, pings, high, monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    out = gate.handle(payload(tmp_path))
    assert out['decision'] == 'block' and 'AUTO-RESUME' not in out['reason']
    assert not (state_dir(tmp_path) / 'watcher.json').exists()
    assert 'chat' not in read_json(state_dir(tmp_path) / 'STOP.json')


def test_second_stop_replans_the_running_watcher(tmp_path, pings, high, monkeypatch):
    gate.handle(payload(tmp_path))
    first = read_json(state_dir(tmp_path) / 'watcher.json')
    monkeypatch.setattr(gate.resume_core, 'pid_alive', lambda pid: True)
    (state_dir(tmp_path) / 'STOP.json').unlink()
    high[0]['resets_at'] += 7200
    gate.handle(payload(tmp_path))
    second = read_json(state_dir(tmp_path) / 'watcher.json')
    assert second['token'] == first['token'] and second['fire_at'] > first['fire_at']


# --------------------------------------------------------------------------- the watcher

class Clock:
    def __init__(self):
        self.t = time.time()

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def start_watcher(tmp_path, fire_in=10):
    write_json(state_dir(tmp_path) / 'watcher.json',
               dict(token='tok', session_id=SESSION, fire_at=time.time() + fire_in, pid=1))
    write_json(state_dir(tmp_path) / 'STOP.json', dict(session_id=SESSION, mode='hard', resets_at=time.time(),
               windows=[dict(limit_id='codex', window='primary', minutes=300, pct=96, resets_at=time.time())]))


def test_watcher_waits_rechecks_then_queues_the_resume(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_QUOTA_LANG', 'tr')
    start_watcher(tmp_path)
    clock, sent, queued = Clock(), [], []
    readings = iter([
        [dict(pct=97, resets_at=clock.t + 600, minutes=300, limit_id='codex', window='primary')],  # reset came late
        [dict(pct=4, resets_at=clock.t + 18000, minutes=300, limit_id='codex', window='primary')],
    ])
    result = watcher.run(tmp_path, 'tok', sleep=clock.sleep, now=clock.now, usage=lambda use_cache: next(readings),
                         queue=lambda sid, cwd: queued.append(sid) or True,
                         ping=lambda text, title: sent.append((title, text)))
    assert result == 'queued' and queued == [SESSION]
    assert sent == [('codex-quota-guard: reset',
                     '🔋 Kota yenilendi (5 saatlik session şu an %4) — sohbet otomatik sürdürülüyor.')]
    assert not (state_dir(tmp_path) / 'STOP.json').exists()
    assert not (state_dir(tmp_path) / 'watcher.json').exists()
    assert read_json(state_dir(tmp_path) / 'chat_resume.json')['session_id'] == SESSION


def test_watcher_reports_a_failed_queue(tmp_path):
    start_watcher(tmp_path, fire_in=0)
    sent = []
    watcher.run(tmp_path, 'tok', sleep=lambda s: None, usage=lambda use_cache: [],
                queue=lambda sid, cwd: False, ping=lambda text, title: sent.append(title))
    assert sent == ['codex-quota-guard: reset', 'codex-quota-guard: warning']


def test_watcher_stops_when_superseded_or_cancelled(tmp_path):
    start_watcher(tmp_path)
    assert watcher.run(tmp_path, 'other-token', usage=None, queue=None, ping=None) == 'superseded'
    (state_dir(tmp_path) / 'watcher.json').unlink()
    assert watcher.run(tmp_path, 'tok', usage=None, queue=None, ping=None) == 'superseded'


def test_watcher_gives_up_after_many_checks(tmp_path):
    start_watcher(tmp_path, fire_in=0)
    clock, sent = Clock(), []

    def still_high(use_cache):
        return [dict(pct=99, resets_at=clock.t + 600, minutes=300, limit_id='codex', window='primary')]

    assert watcher.run(tmp_path, 'tok', sleep=clock.sleep, now=clock.now, usage=still_high,
                       queue=None, ping=lambda text, title: sent.append(title)) == 'gave-up'
    assert sent == ['codex-quota-guard: warning']


def test_queue_command_targets_the_same_thread(tmp_path, monkeypatch, fake_cli):
    log = tmp_path / 'queue.log'
    monkeypatch.setenv('FAKE_QUEUE_LOG', str(log))
    monkeypatch.setattr(watcher, 'codex_command', lambda: fake_cli)
    assert watcher.queue_message(SESSION, str(tmp_path)) is True
    args = json.loads(log.read_text(encoding='utf-8').splitlines()[0])
    assert args[:2] == ['--thread', SESSION] and args[2] == '--message' and watcher.RESUME_TAG in args[3]
    monkeypatch.setenv('FAKE_MODE', 'queue-fail')
    assert watcher.queue_message(SESSION, str(tmp_path)) is False


# --------------------------------------------------------------------------- after the wake-up

def test_resumed_turn_pings_working_once_then_done(tmp_path, pings, monkeypatch):
    monkeypatch.setenv('CODEX_QUOTA_LANG', 'tr')
    monkeypatch.setattr(gate, 'get_usage', lambda: [])
    folder = state_dir(tmp_path)
    folder.mkdir()
    gate.resume_core.write_state(str(folder), SESSION)
    gate.handle(payload(tmp_path))
    gate.handle(payload(tmp_path))
    gate.handle(payload(tmp_path, session_id='someone-else'))
    assert pings == [('codex-quota-guard: resumed', '▶️ Codex kaldığı yerden çalışmaya devam ediyor.')]
    (folder / 'progress.md').write_text('all done\nCODEX_QUOTA_DONE\n', encoding='utf-8')
    assert gate.handle(payload(tmp_path, 'Stop')) == {}
    assert pings[-1] == ('codex-quota-guard: done', '✅ Bitti — Codex kaldığı işi tamamladı.')
    gate.handle(payload(tmp_path, 'Stop'))
    assert len(pings) == 2  # the cycle is closed
