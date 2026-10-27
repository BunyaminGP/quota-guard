"""Run and resume one exact Codex thread at subscription quota resets."""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import uuid

from common import choose_stop, config, git, message, read_json, state_dir, write_json
from notify import notify
from process import codex_command, lines, start, stop
from usage import get_usage

RULES = '''
Maintain .codex-quota/progress.md with completed work, pending steps and resume context.
On resume also read .codex-quota/todos_state.json; redo any item marked pending after rollback.
Use update_plan to track task boundaries. When a quota hook asks you to stop, write the
checkpoint and end the turn. Preserve user changes. Commit only when authorized by the user.
When ALL requested work is finished, put CODEX_QUOTA_DONE on its own line in progress.md.
'''


def run_round(command, prompt, timeout, on_thread, cwd, env):
    proc = start(command, cwd=cwd, env=env, stderr=subprocess.STDOUT)
    events = lines(proc)
    deadline = time.monotonic() + timeout if timeout else float('inf')
    failed = False
    completed = False

    def feed():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    threading.Thread(target=feed, daemon=True).start()
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 124
            try:
                line = events.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                event = json.loads(line)
            except ValueError:
                print(line.rstrip(), flush=True)
                continue
            kind = event.get('type')
            if kind == 'thread.started' and isinstance(event.get('thread_id'), str):
                on_thread(event['thread_id'])
            elif kind == 'turn.failed':
                failed = True
            elif kind == 'turn.completed':
                completed = True
            elif kind == 'item.completed':
                item = event.get('item', {})
                if item.get('type') == 'agent_message':
                    print(item.get('text', ''), flush=True)
                elif item.get('type'):
                    print('[tool] ' + item['type'], flush=True)
            elif kind == 'error':
                print(event.get('message', 'Codex error'), file=sys.stderr)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 124
        try:
            code = proc.wait(timeout=None if math.isinf(remaining) else remaining)
        except subprocess.TimeoutExpired:
            return 124
        return code or (1 if failed or not completed else 0)
    finally:
        stop(proc)
        proc.stdout.close()


def build_command(base, thread_id, model):
    command = base + ['exec', '-c', 'sandbox_mode="workspace-write"']
    if thread_id:
        command += ['resume', thread_id]
    if model:
        command += ['--model', model]
    return command + ['--json', '-']


def done(folder):
    path = folder / 'progress.md'
    return not path.is_symlink() and path.is_file() and 'CODEX_QUOTA_DONE' in path.read_text(encoding='utf-8').splitlines()


def wait_reset(marker, buffer, language):
    reset = marker['resets_at']
    if not isinstance(reset, (int, float)) or not math.isfinite(reset):
        raise RuntimeError('Missing quota reset time; cannot schedule a resume')
    text = message('waiting', language, reset=datetime.fromtimestamp(reset, timezone.utc).isoformat())
    print(text, flush=True)
    while True:
        remaining = reset + buffer - time.time()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 30))


def execute(args):
    project = Path.cwd()
    folder = state_dir(project)
    folder.mkdir(exist_ok=True)
    lock = folder / 'runner.lock'
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RuntimeError('A runner lock exists; stop the other runner before removing .codex-quota/runner.lock') from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return run_loop(args, project, folder)
    finally:
        lock.unlink(missing_ok=True)


def run_loop(args, project, folder):
    for name in ('run.json', 'STOP.json', 'todos_state.json', 'progress.md'):
        if git(project, 'ls-files', '--error-unmatch', '.codex-quota/' + name)[0] == 0:
            raise RuntimeError('Runtime state must be untracked: .codex-quota/' + name)
    for key in ('session_soft', 'session_hard', 'weekly_soft', 'weekly_hard'):
        if getattr(args, key) is not None:
            os.environ['CODEX_QUOTA_' + key.upper()] = str(getattr(args, key))
    if args.enable_hard_abort:
        os.environ['CODEX_QUOTA_HARD_ABORT'] = '1'
    if args.lang:
        os.environ['CODEX_QUOTA_LANG'] = args.lang
    if args.limit_id:
        os.environ['CODEX_QUOTA_LIMIT_ID'] = args.limit_id
    cfg = config(project)
    if args.resume:
        state = read_json(folder / 'run.json')
        if not state.get('thread_id') or not state.get('task'):
            raise RuntimeError('No saved thread/task to resume')
        if not args.model:
            args.model = state.get('model')
    else:
        if not args.task:
            raise RuntimeError('Provide a task or --resume')
        task = Path(args.task[1:]).read_text(encoding='utf-8') if args.task.startswith('@') else args.task
        state = dict(run_id=str(uuid.uuid4()), thread_id=None, task=task, model=args.model)
        progress = folder / 'progress.md'
        if progress.is_symlink():
            raise RuntimeError('Unsafe progress.md symlink')
        if progress.exists():
            progress.rename(folder / ('progress-' + uuid.uuid4().hex + '.md'))
        (folder / 'STOP.json').unlink(missing_ok=True)
        (folder / 'todos_state.json').unlink(missing_ok=True)
    write_json(folder / 'run.json', state)
    env = dict(os.environ, CODEX_QUOTA_RUN_ID=state['run_id'])
    base = codex_command()
    failures = 0

    def on_thread(identifier):
        if state['thread_id'] and state['thread_id'] != identifier:
            raise RuntimeError('Codex returned a different thread ID')
        state['thread_id'] = identifier
        write_json(folder / 'run.json', state)

    while True:
        marker = read_json(folder / 'STOP.json')
        if marker.get('run_id') == state['run_id'] and marker.get('resets_at', 0) + args.resume_buffer > time.time():
            wait_reset(marker, args.resume_buffer, cfg['language'])
        (folder / 'STOP.json').unlink(missing_ok=True)
        try:
            marker = choose_stop(get_usage(use_cache=False), cfg)
        except (OSError, ValueError, RuntimeError):
            marker = None
            print('Quota unavailable; continuing without a preflight quota reading.', file=sys.stderr)
        if marker:
            notify(message('stop', cfg['language'], reset=datetime.fromtimestamp(
                marker['resets_at'], timezone.utc).isoformat()))
            wait_reset(marker, args.resume_buffer, cfg['language'])
            continue  # refresh all windows after waking, before spending another turn
        if state['thread_id']:
            text = message('resume', cfg['language'])
            print(text, flush=True)
            notify(text)
            prompt = 'Read .codex-quota/progress.md and continue the original task.\n' + RULES
        else:
            prompt = state['task'] + '\n' + RULES
        result = run_round(build_command(base, state['thread_id'], args.model), prompt,
                           args.round_timeout, on_thread, str(project), env)
        if result == 0 and done(folder):
            text = message('done', cfg['language'])
            print(text)
            notify(text)
            return 0
        marker = read_json(folder / 'STOP.json')
        if marker.get('run_id') == state['run_id'] and marker.get('session_id') == state['thread_id']:
            if not state['thread_id']:
                raise RuntimeError('Quota stop without a thread ID; refusing an ambiguous resume')
            if result == 124:
                print('Round timed out; checkpoint may be incomplete.', file=sys.stderr)
            wait_reset(marker, args.resume_buffer, cfg['language'])
            (folder / 'STOP.json').unlink(missing_ok=True)
            failures = 0
            continue
        if result == 0:
            raise RuntimeError('Codex ended without a completion marker or quota stop; inspect progress before --resume')
        failures += 1
        if failures > args.max_retries:
            notify(message('failed', cfg['language']))
            return 1
        print(f'Round failed ({result}); retry {failures}/{args.max_retries}.', file=sys.stderr)
        time.sleep(args.retry_backoff * failures)


def nonnegative(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError('Expected a nonnegative finite number')
    return number


def percentage(value):
    number = nonnegative(value)
    if not 0 < number <= 100:
        raise argparse.ArgumentTypeError('Expected a percentage in (0, 100]')
    return number


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('task', nargs='?', help='Task text or @file.md')
    p.add_argument('--resume', action='store_true', help='Resume the exact saved thread in this directory')
    p.add_argument('--model')
    p.add_argument('--limit-id', help='Explicit app-server billing bucket; default checks all returned buckets')
    p.add_argument('--threshold', '--session-soft', dest='session_soft', type=percentage)
    for name in ('session-hard', 'weekly-soft', 'weekly-hard'):
        p.add_argument('--' + name, type=percentage)
    p.add_argument('--enable-hard-abort', action='store_true')
    p.add_argument('--lang', choices=['en', 'tr'])
    p.add_argument('--round-timeout', type=nonnegative, default=3600, help='Seconds; 0 disables timeout')
    p.add_argument('--resume-buffer', type=nonnegative, default=60)
    p.add_argument('--max-retries', type=int, default=5)
    p.add_argument('--retry-backoff', type=nonnegative, default=30)
    return p


def main():
    args = parser().parse_args()
    if args.max_retries < 0 or (args.resume and args.task):
        parser().error('Invalid retry count or task combined with --resume')
    try:
        return execute(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        if isinstance(exc, RuntimeError):
            notify(message('failed', args.lang or 'en'))
        return 1


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    raise SystemExit(main())
