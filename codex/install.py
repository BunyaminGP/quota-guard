"""Register this checkout's hooks, preserving other Codex hooks and trust settings."""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
from common import read_json, write_json


def install(home):
    script = str(Path(__file__).resolve().parent / 'scripts' / 'quota_gate.py')
    command = subprocess.list2cmdline([sys.executable, script]) if os.name == 'nt' else shlex.join([sys.executable, script])
    path = home / 'hooks.json'
    if path.is_symlink():
        raise ValueError('Refusing to replace a hooks.json symlink')
    if path.exists():
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('Invalid existing hooks.json')
    else:
        value = {}
    hooks = value.setdefault('hooks', {})
    for event in ('PostToolUse', 'Stop'):
        entries = hooks.setdefault(event, [])
        if any(handler.get('command') == command for entry in entries for handler in entry.get('hooks', [])):
            continue
        item = dict(hooks=[dict(type='command', command=command, timeout=30)])
        if event == 'PostToolUse':
            item['matcher'] = '*'
        entries.append(item)
    write_json(path, value)
    return path


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', type=Path,
                        default=Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')))
    args = parser.parse_args()
    try:
        print('Registered:', install(args.codex_home))
        print('Open Codex /hooks and review and trust these hook definitions before running a task.')
        print('Keep this checkout at its current path. Run bin/codex-run or bin/codex-run.ps1.')
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
