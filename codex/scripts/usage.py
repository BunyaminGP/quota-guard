"""Read subscription limits through the official app-server RPC, never auth files."""
import json
import math
import os
from pathlib import Path
import queue
import sys
import time

from common import read_json, write_json
from process import codex_command, lines, start, stop


def fetch_raw(timeout=12, command=None):
    proc = start((command or codex_command()) + ['app-server', '--listen', 'stdio://'])
    events = lines(proc)
    deadline = time.monotonic() + timeout

    def send(value):
        proc.stdin.write(json.dumps(value) + '\n')
        proc.stdin.flush()

    def response(identifier):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Quota RPC timed out')
            try:
                line = events.get(timeout=remaining)
            except queue.Empty:
                raise RuntimeError('Quota RPC timed out') from None
            if line is None:
                raise RuntimeError('Quota RPC closed before responding')
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if item.get('id') != identifier:
                continue
            if 'error' in item:
                raise RuntimeError('Quota RPC unavailable; sign in to Codex with ChatGPT')
            return item.get('result', {})

    try:
        send(dict(id=1, method='initialize', params=dict(
            clientInfo=dict(name='codex_quota_guard', version='0.1.0'),
            capabilities=dict(experimentalApi=False))))
        response(1)
        send(dict(method='initialized', params={}))
        send(dict(id=2, method='account/rateLimits/read', params={}))
        return response(2)
    finally:
        stop(proc)
        proc.stdin.close()
        proc.stdout.close()


def normalize(raw, limit_id=None):
    buckets = raw.get('rateLimitsByLimitId') or {}
    if not buckets and isinstance(raw.get('rateLimits'), dict):
        bucket = raw['rateLimits']
        buckets = {bucket.get('limitId') or 'codex': bucket}
    if limit_id:
        buckets = {limit_id: buckets[limit_id]} if limit_id in buckets else {}
    windows = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for name in ('primary', 'secondary'):
            w = bucket.get(name)
            if not isinstance(w, dict):
                continue
            try:
                pct, reset, minutes = float(w['usedPercent']), float(w['resetsAt']), float(w['windowDurationMins'])
                if not all(math.isfinite(n) for n in (pct, reset, minutes)) or not 0 <= pct <= 100 or minutes <= 0:
                    continue
            except (KeyError, TypeError, ValueError):
                continue
            windows.append(dict(limit_id=key, window=name, pct=pct, resets_at=reset, minutes=minutes))
    if not windows:
        raise RuntimeError('No usable subscription quota windows were returned')
    return windows


def get_usage(use_cache=True):
    # An explicit limit ID is used rather than guessing billing groups from model names.
    limit_id = os.environ.get('CODEX_QUOTA_LIMIT_ID') or None
    home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
    cache_path = home / 'codex-quota-cache.json'
    cache = read_json(cache_path)
    if use_cache and cache.get('limit_id') == limit_id and 0 <= time.time() - cache.get('fetched_at', 0) < 30:
        return normalize(cache.get('raw', {}), limit_id)
    raw = fetch_raw()
    windows = normalize(raw, limit_id)
    try:
        write_json(cache_path, dict(raw=raw, fetched_at=time.time(), limit_id=limit_id))
    except OSError:
        pass
    return windows


if __name__ == '__main__':
    try:
        print(json.dumps(get_usage(use_cache='--no-cache' not in sys.argv), indent=2))
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
