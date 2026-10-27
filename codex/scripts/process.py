"""Launch the CLI without passing prompts through a command shell."""
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import threading


def codex_command():
    executable = os.environ.get('CODEX_QUOTA_CLI') or shutil.which('codex')
    if not executable:
        raise RuntimeError('codex CLI not found')
    path = Path(executable)
    if path.suffix.lower() in ('.cmd', '.bat', '.ps1'):
        # npm's shim uses a shell; invoke its Node entry point directly instead.
        entry = path.parent / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
        node = shutil.which('node')
        if not entry.is_file() or not node:
            raise RuntimeError('Set CODEX_QUOTA_CLI to the native codex executable')
        return [node, str(entry)]
    return [str(path)]


def start(command, **kwargs):
    options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   text=True, encoding='utf-8', errors='replace', bufsize=1)
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options['start_new_session'] = True
    options.update(kwargs)
    return subprocess.Popen(command, **options)


def stop(proc):
    if os.name == 'nt':
        if proc.poll() is None:
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def lines(proc):
    events = queue.Queue()

    def read():
        try:
            for line in proc.stdout:
                events.put(line)
        finally:
            events.put(None)

    threading.Thread(target=read, daemon=True).start()
    return events
