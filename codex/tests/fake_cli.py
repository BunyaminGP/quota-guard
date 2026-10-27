"""Subprocess fixture: no network or real model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')

mode = os.environ.get('FAKE_MODE', 'done')


def emit(value):
    print(json.dumps(value), flush=True)


if sys.argv[1:2] == ['queue']:
    with open(os.environ['FAKE_QUEUE_LOG'], 'a', encoding='utf-8') as out:
        out.write(json.dumps(sys.argv[2:]) + '\n')
    sys.exit(1 if mode == 'queue-fail' else 0)
elif 'app-server' in sys.argv:
    for line in sys.stdin:
        request = json.loads(line)
        if request['method'] == 'initialize':
            emit({'method': 'notification', 'params': {}})
            emit({'id': 1, 'result': {}})
        elif request['method'] == 'account/rateLimits/read':
            if mode == 'rpc-error':
                emit({'id': 2, 'error': {'message': 'secret should never be echoed'}})
            elif mode == 'hang':
                time.sleep(60)
            else:
                emit({'id': 2, 'result': {'rateLimits': {'limitId': 'codex', 'primary': {
                    'usedPercent': 41, 'windowDurationMins': 300, 'resetsAt': time.time() + 60}}}})
else:
    prompt = sys.stdin.read()
    folder = Path('.codex-quota')
    folder.mkdir(exist_ok=True)
    calls = folder / 'calls.jsonl'
    previous = calls.read_text().splitlines() if calls.exists() else []
    with calls.open('a') as out:
        out.write(json.dumps({'argv': sys.argv[1:], 'prompt': prompt}) + '\n')
    if mode == 'first-fail' and not previous:
        sys.exit(1)
    if mode == 'always-fail':
        sys.exit(1)
    if mode != 'no-id':
        emit({'type': 'thread.started', 'thread_id': 'thread-test-123'})
    if mode == 'hang':
        # Child would write outside the round after timeout if process-tree cleanup failed.
        subprocess.Popen([sys.executable, '-c',
                          "import time,pathlib; time.sleep(2); pathlib.Path('child-survived').touch()"])
        time.sleep(60)
    if mode in ('stop-resume', 'no-id') and not previous:
        (folder / 'progress.md').write_text('Checkpoint: step two remains.', encoding='utf-8')
        (folder / 'STOP.json').write_text(json.dumps(dict(
            mode='soft', resets_at=time.time() + 0.05, session_id='thread-test-123',
            run_id=os.environ['CODEX_QUOTA_RUN_ID'])))
    elif mode == 'logical-failure':
        emit({'type': 'turn.failed', 'error': {'message': 'failed'}})
        sys.exit(0)
    elif mode != 'unfinished':
        (folder / 'progress.md').write_text('CODEX_QUOTA_DONE\n', encoding='utf-8')
    emit({'type': 'turn.completed', 'usage': {}})
