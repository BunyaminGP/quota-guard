"""Codex PostToolUse checkpoint feedback and Stop completion guard."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

import uuid

from common import choose_stop, config, git, message, read_json, resume_core, state_dir, write_json
from notify import notify
from usage import get_usage

DONE_LINE = 'CODEX_QUOTA_DONE'
WATCHER = str(Path(__file__).resolve().parent / 'resume_watcher.py')


def tracked(project, name):
    return git(project, 'ls-files', '--error-unmatch', '.codex-quota/' + name)[0] == 0


HOOK_LOCK_STALE = 60  # seconds; hooks time out after 30


def runner_active(project, folder):
    """A codex-run runner really owns this project: runner.lock is untracked
    and holds a live PID. A lock that came with a cloned repo (or one a crash
    left behind) must not switch the guard off for every chat."""
    lock = folder / 'runner.lock'
    if lock.is_symlink() or not lock.is_file() or tracked(project, 'runner.lock'):
        return False
    try:
        pid = int(lock.read_text(encoding='utf-8').strip() or 0)
    except (OSError, ValueError):
        return False
    return resume_core.pid_alive(pid)


def take_hook_lock(project, folder):
    """O_EXCL lock so parallel PostToolUse hooks don't overwrite each other.
    A tracked (planted) or stale lock is removed instead of making every
    hook skip forever."""
    lock = folder / 'hook.lock'
    for _ in range(2):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return lock
        except FileExistsError:
            try:
                age = time.time() - lock.lstat().st_mtime
            except OSError:
                continue
            if age < HOOK_LOCK_STALE and not lock.is_symlink() and not tracked(project, 'hook.lock'):
                return None  # another hook is running right now
            lock.unlink(missing_ok=True)
    return None


def plan_state(project, previous, plan, session_id):
    tasks = [dict(content=p['step'], status=p['status']) for p in plan
             if isinstance(p, dict) and isinstance(p.get('step'), str)
             and p.get('status') in ('pending', 'in_progress', 'completed')]
    active = next((p['content'] for p in tasks if p['status'] == 'in_progress'), None)
    old = previous.get('in_progress') or {}
    checkpoint = old if old.get('content') == active else None
    if active and not checkpoint:
        code, head = git(project, 'rev-parse', 'HEAD')
        status, changes = git(project, 'status', '--porcelain', '--untracked-files=all',
                              '--', '.', ':(exclude).codex-quota')
        checkpoint = dict(content=active, commit=head if code == 0 else None,
                          clean=code == 0 and status == 0 and not changes)
    return dict(session_id=session_id, todos=tasks, in_progress=checkpoint if active else None)


def revert_item(project, state):
    active = state.get('in_progress') or {}
    if not active.get('clean') or not active.get('commit') or tracked(project, 'todos_state.json'):
        return False
    code, head = git(project, 'rev-parse', 'HEAD')
    if code or head != active['commit']:
        return False
    code, changed = git(project, 'diff', '--relative', '--name-only', '-z', 'HEAD', raw=True)
    other_code, untracked = git(project, 'ls-files', '--others', '--exclude-standard', '-z', raw=True)
    if code or other_code:
        return False
    paths = sorted({p for p in (changed + untracked).split('\0')
                    if p and p != '.codex-quota' and not p.startswith('.codex-quota/')})
    if not paths:
        return False
    _, before = git(project, 'rev-parse', '--verify', 'refs/stash')
    code, _ = git(project, 'stash', 'push', '-u', '-m', 'codex-quota-guard checkpoint',
                  '--pathspec-from-file=-', '--pathspec-file-nul',
                  input_data=''.join(':(literal)' + p + '\0' for p in paths))
    _, after = git(project, 'rev-parse', '--verify', 'refs/stash')
    if code or not after or before == after:
        return False
    for task in state.get('todos', []):
        if task.get('content') == active['content']:
            task['status'] = 'pending'
    state['in_progress'] = None
    return True


def feedback(marker, event='PostToolUse'):
    reset = datetime.fromtimestamp(marker['resets_at'], timezone.utc).isoformat()
    instruction = (
        f"Quota {marker['mode']} threshold reached; reset {reset}. "
        "Do not start another task. Save completed work, pending steps and resume context to "
        ".codex-quota/progress.md, then END THE TURN. Preserve existing user changes. "
        "Commit only if the user's task authorizes commits. Do not write CODEX_QUOTA_DONE "
        "unless the entire requested task is complete."
    )
    if marker.get('reverted'):
        instruction += ' The current item was stashed successfully; it is pending and must be redone after reset.'
    if marker.get('chat'):
        instruction += (f" AUTO-RESUME: the guard will wake this chat by itself at {marker.get('resume_display')} "
                        "(local time) by queueing a resume message, as long as this Codex window stays open. "
                        "Tell the user that in one line, then end the turn.")
    return dict(decision='block', reason=instruction, hookSpecificOutput=dict(
        hookEventName=event, additionalContext=instruction)) if event == 'PostToolUse' else dict(
            decision='block', reason=instruction)


# --------------------------------------------------------------------------- chat auto-resume

def spawn_watcher(project, session_id, plan):
    """Start (or re-plan) the background process that wakes this chat after the reset."""
    folder = state_dir(project)
    current = read_json(folder / 'watcher.json')
    if current.get('session_id') == session_id and resume_core.pid_alive(current.get('pid')):
        current['fire_at'] = plan['fire_at']
        write_json(folder / 'watcher.json', current)
        return current.get('pid')
    data = dict(token=uuid.uuid4().hex, session_id=session_id, fire_at=plan['fire_at'], started_at=time.time())
    write_json(folder / 'watcher.json', data)
    data['pid'] = resume_core.spawn_detached(
        [resume_core.python_exe(), WATCHER, '--project', str(project), '--token', data['token']],
        log_path=str(folder / 'watcher.log'))
    write_json(folder / 'watcher.json', data)
    return data['pid']


def chat_working(folder, session_id, cfg):
    """First tool call of the resumed turn -> 'working again' (once)."""
    if resume_core.read_state(str(folder), session_id) and resume_core.claim_once(
            resume_core.working_flag_path(str(folder))):
        notify(message('chat_working', cfg['language']), title='codex-quota-guard: resumed')


def chat_done(folder, session_id, cfg):
    """End of the resumed turn -> 'done' (or 'turn finished'), then the cycle is closed."""
    state = resume_core.read_state(str(folder), session_id)
    if not state:
        return
    key = 'chat_done' if resume_core.progress_done_since(str(folder), state['resumed_at'], DONE_LINE) else 'chat_turn_done'
    notify(message(key, cfg['language']), title='codex-quota-guard: done')
    resume_core.clear_state(str(folder))


def handle(payload):
    project = Path(payload.get('cwd') or os.getcwd())
    folder = state_dir(project)
    session_id = payload.get('session_id')
    if not session_id:
        return {}
    run = read_json(folder / 'run.json')
    run_id = os.environ.get('CODEX_QUOTA_RUN_ID')
    if run_id != run.get('run_id') and runner_active(project, folder):
        return {}
    if run_id and run.get('thread_id') not in (None, session_id):
        return {}
    # A cloned runtime file must never claim a checkpoint or stop for this session.
    for name in ('todos_state.json', 'STOP.json'):
        if tracked(project, name):
            return {}
    marker = read_json(folder / 'STOP.json')
    same_marker = marker.get('session_id') == session_id and marker.get('run_id') == run_id
    event = payload.get('hook_event_name', 'PostToolUse')
    if event == 'Stop':
        if same_marker and marker.get('resets_at', 0) > time.time():
            # One continuation gives the model a chance to persist its checkpoint.
            progress = folder / 'progress.md'
            updated = (not progress.is_symlink() and progress.is_file()
                       and progress.stat().st_mtime_ns != marker.get('progress_mtime'))
            if not payload.get('stop_hook_active') and not updated:
                return feedback(marker, 'Stop')
            return {}
        if not run_id:
            chat_done(folder, session_id, config(project))
        return {}
    if event != 'PostToolUse':
        return {}
    if not run_id:
        chat_working(folder, session_id, config(project))
    if same_marker and marker.get('resets_at', 0) > time.time():
        return {}  # allow checkpoint writes after the initial instruction
    previous = read_json(folder / 'todos_state.json')
    if previous.get('session_id') != session_id:
        previous = {}
    state = previous
    tool = payload.get('tool_name', '').split('.')[-1]
    soft_allowed = not previous.get('in_progress')
    args = payload.get('tool_input') or {}
    if tool == 'update_plan' and isinstance(args, dict) and isinstance(args.get('plan'), list):
        state = plan_state(project, previous, args['plan'], session_id)
        soft_allowed = state.get('in_progress') != previous.get('in_progress') or not state.get('in_progress')
        write_json(folder / 'todos_state.json', state)
    cfg = config(project)
    marker = choose_stop(get_usage(), cfg, soft_allowed=soft_allowed)
    if not marker:
        return {}
    marker.update(session_id=session_id, run_id=run_id, reverted=False)
    plan = None
    if not run_id and cfg['chat_auto_resume']:
        # A normal interactive chat (no codex-run): wake it up again after the reset.
        resume_core.archive_done_progress(str(folder), DONE_LINE)
        resume_core.clear_state(str(folder))
        which = 'weekly' if any(w['minutes'] >= 10080 for w in marker['windows']) else 'session'
        plan = resume_core.plan_resume(marker['resets_at'], which, buffer_seconds=cfg['resume_buffer'])
        marker.update(chat=True, resume_at=plan['fire_at'], resume_display=plan['display'])
    progress = folder / 'progress.md'
    marker['progress_mtime'] = progress.stat().st_mtime_ns if progress.is_file() and not progress.is_symlink() else None
    if marker['mode'] == 'hard' and cfg['hard_abort']:
        marker['reverted'] = revert_item(project, state)
        if marker['reverted']:
            write_json(folder / 'todos_state.json', state)
    write_json(folder / 'STOP.json', marker)
    reset = datetime.fromtimestamp(marker['resets_at'], timezone.utc).isoformat()
    text = message('stop', cfg['language'], reset=reset)
    if plan:
        spawn_watcher(project, session_id, plan)
        text += message('chat_suffix', cfg['language'], time=plan['display'])
    notify(text)
    result = feedback(marker)
    result['systemMessage'] = text
    return result


def main():
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError('Invalid hook input')
        # Parallel PostToolUse hooks must not overwrite each other's checkpoint.
        project = Path(payload.get('cwd') or os.getcwd())
        folder = state_dir(project)
        folder.mkdir(exist_ok=True)
        lock = take_hook_lock(project, folder)
        if lock is None:
            print('{}')
            return
        try:
            result = handle(payload)
        finally:
            lock.unlink(missing_ok=True)
        print(json.dumps(result, ensure_ascii=True))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        print('{}')  # quota/config/service errors must not break an ordinary Codex session


if __name__ == '__main__':
    main()
