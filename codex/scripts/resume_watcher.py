"""Wakes a stopped Codex chat once the quota has reset (chat mode, no codex-run).

Started in the background by quota_gate.py when a threshold stops a normal
interactive chat. It sleeps until the planned time, re-reads the quota, and:

  still over a threshold -> plans the next check (the reset came late)
  reset                  -> "quota reset" ping + `codex queue --thread <id>`
                            with RESUME_PROMPT, which Codex runs as the next
                            turn of that same chat once it is idle

Codex has no model-side timer, so this process is the timer. Its plan lives
in .codex-quota/watcher.json: a newer stop rewrites the plan (this process
picks it up), and deleting the file cancels it.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import choose_stop, config, message, read_json, resume_core, state_dir, write_json  # noqa: E402
from notify import notify  # noqa: E402
from process import codex_command  # noqa: E402
from usage import get_usage  # noqa: E402

RESUME_TAG = '[codex-quota-guard:auto-resume]'
DONE_LINE = 'CODEX_QUOTA_DONE'
RESUME_PROMPT = (
    RESUME_TAG + ' The usage quota has reset. Continue the task you were working on before the '
    'quota stop: read .codex-quota/progress.md to see where you left off, then keep going from there. '
    'If the task is already complete, say so briefly and end the turn. When the whole task is '
    'finished, write ' + DONE_LINE + ' on its own line in .codex-quota/progress.md.'
)
MAX_CHECKS = 24          # quota still not reset after this many wake-ups -> give up and tell the user
RETRY_SECONDS = 300      # quota unreadable -> try again in 5 minutes


def watcher_path(project):
    return state_dir(project) / 'watcher.json'


def label(window, language):
    return message('weekly' if window.get('minutes', 0) >= 10080 else 'session', language)


def queue_message(session_id, cwd):
    cmd = codex_command() + ['queue', '--thread', session_id, '--message', RESUME_PROMPT]
    try:
        r = subprocess.run(cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=120)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def run(project, token, sleep=time.sleep, now=time.time, usage=get_usage, queue=queue_message, ping=notify):
    project = Path(project)
    folder = state_dir(project)
    checks = 0
    while True:
        plan = read_json(watcher_path(project))
        if plan.get('token') != token:
            return 'superseded'                          # a newer stop owns the wake-up now
        session_id = plan.get('session_id')
        wait = float(plan.get('fire_at', 0)) - now()
        if wait > 0:
            sleep(min(wait, 30))
            continue
        cfg = config(project)
        lang = cfg['language']
        try:
            windows = usage(use_cache=False)
        except (OSError, ValueError, RuntimeError):
            windows = None
        checks += 1
        if windows is None or (stop := choose_stop(windows, cfg, soft_allowed=True)):
            if checks >= MAX_CHECKS:
                ping(message('chat_gave_up', lang), title='codex-quota-guard: warning')
                watcher_path(project).unlink(missing_ok=True)
                return 'gave-up'
            nxt = (resume_core.plan_resume(stop['resets_at'], now=now(), buffer_seconds=cfg['resume_buffer'])['fire_at']
                   if windows is not None else now() + RETRY_SECONDS)
            plan['fire_at'] = nxt
            write_json(watcher_path(project), plan)
            continue
        # The quota has reset: hand over to the hook ("working again" / "done") and wake the chat.
        marker = read_json(folder / 'STOP.json')
        resume_core.write_state(str(folder), session_id)
        if marker.get('session_id') == session_id:
            (folder / 'STOP.json').unlink(missing_ok=True)
        stopped = (marker.get('windows') or [{}])[0]
        fresh = next((w for w in windows if w.get('limit_id') == stopped.get('limit_id')
                      and w.get('window') == stopped.get('window')), None)
        text = (message('chat_reset', lang, label=label(fresh, lang), pct=f"{fresh['pct']:.0f}") if fresh
                else message('chat_reset_nopct', lang))
        ping(text, title='codex-quota-guard: reset')
        if not queue(session_id, str(project)):
            ping(message('chat_queue_failed', lang), title='codex-quota-guard: warning')
        watcher_path(project).unlink(missing_ok=True)
        return 'queued'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--token', required=True)
    args = parser.parse_args()
    try:
        print(time.strftime('%H:%M:%S'), 'watcher', run(args.project, args.token), flush=True)
    except Exception as exc:  # a background helper must never leave a traceback dialog behind
        print('watcher error:', exc, file=sys.stderr, flush=True)


if __name__ == '__main__':
    main()
