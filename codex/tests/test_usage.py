import time

import pytest

from common import choose_stop, DEFAULTS
import usage


def bucket(pct, reset, minutes=300):
    return dict(primary=dict(usedPercent=pct, resetsAt=reset, windowDurationMins=minutes))


def test_rpc_handshake_with_notification(fake_cli):
    raw = usage.fetch_raw(command=fake_cli)
    assert usage.normalize(raw)[0]['pct'] == 41


def test_rpc_errors_redacted(fake_cli, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'rpc-error')
    with pytest.raises(RuntimeError, match='Quota RPC unavailable') as error:
        usage.fetch_raw(command=fake_cli)
    assert 'secret' not in str(error.value)


def test_rpc_timeout(fake_cli, monkeypatch):
    monkeypatch.setenv('FAKE_MODE', 'hang')
    started = time.monotonic()
    with pytest.raises(RuntimeError, match='timed out'):
        usage.fetch_raw(command=fake_cli, timeout=0.3)
    assert time.monotonic() - started < 5


def test_all_windows_preserved_and_latest_soft_reset_wins_with_hard():
    windows = usage.normalize({'rateLimitsByLimitId': {
        'codex': bucket(96, 200), 'another': bucket(98, 500, 10080),
        'later': bucket(81, 900)}})
    assert len(windows) == 3
    stop = choose_stop(windows, DEFAULTS, now=100)
    assert stop['mode'] == 'hard'
    assert stop['resets_at'] == 900
    assert len(stop['windows']) == 3
    assert choose_stop(windows, DEFAULTS, soft_allowed=False, now=100)['resets_at'] == 900


def test_explicit_bucket_filter_and_missing_id():
    raw = {'rateLimitsByLimitId': {'codex': bucket(5, 200), 'other': bucket(99, 500)}}
    assert usage.normalize(raw, 'codex')[0]['pct'] == 5
    with pytest.raises(RuntimeError):
        usage.normalize(raw, 'missing')


@pytest.mark.parametrize('pct', [-1, 101, 'nan', 'inf', None])
def test_invalid_readings_never_become_zero(pct):
    with pytest.raises(RuntimeError):
        usage.normalize({'rateLimitsByLimitId': {'codex': bucket(pct, 500)}})


def test_expired_window_does_not_stop():
    assert choose_stop(usage.normalize({'rateLimits': bucket(99, 100)}), DEFAULTS, now=200) is None


def test_cache_and_fresh_probe(monkeypatch):
    calls = []
    def fetch():
        calls.append(1)
        return {'rateLimits': bucket(40, time.time() + 30)}
    monkeypatch.setattr(usage, 'fetch_raw', fetch)
    usage.get_usage()
    usage.get_usage()
    assert len(calls) == 1
    usage.get_usage(use_cache=False)
    assert len(calls) == 2
