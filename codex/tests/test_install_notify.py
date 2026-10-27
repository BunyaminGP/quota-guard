import http.server
import json
from pathlib import Path
import threading

import pytest

import install
import notify


def test_install_idempotent_preserves_existing(tmp_path):
    path = tmp_path / 'hooks.json'
    path.write_text(json.dumps({'hooks': {'SessionStart': [{'hooks': [{'command': 'existing'}]}]}}))
    install.install(tmp_path)
    install.install(tmp_path)
    hooks = json.loads(path.read_text())['hooks']
    assert hooks['SessionStart'][0]['hooks'][0]['command'] == 'existing'
    assert len(hooks['PostToolUse']) == len(hooks['Stop']) == 1
    assert 'bypass' not in path.read_text()


def test_malformed_existing_hooks_are_preserved(tmp_path):
    path = tmp_path / 'hooks.json'
    path.write_text('broken')
    with pytest.raises(ValueError):
        install.install(tmp_path)
    assert path.read_text() == 'broken'


def test_notification_utf8_local_server(monkeypatch):
    bodies = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv('CODEX_QUOTA_NOTIFY_URL', f'http://127.0.0.1:{server.server_port}/topic')
        assert notify.notify('Görev tamamlandı.')
        assert bodies == ['Görev tamamlandı.'.encode()]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_notification_disabled_and_unavailable(monkeypatch):
    assert not notify.notify('test')
    monkeypatch.setenv('CODEX_QUOTA_NOTIFY_URL', 'http://127.0.0.1:1/topic')
    assert not notify.notify('test')


def test_manifests_parse_and_paths_exist():
    root = Path(install.__file__).parent.parent  # the quota-guard plugin root (one plugin for all hosts)
    manifest = json.loads((root / '.codex-plugin' / 'plugin.json').read_text(encoding='utf-8'))
    hooks = root / manifest['hooks']
    assert set(json.loads(hooks.read_text())['hooks']) == {'PostToolUse', 'Stop'}
    for entry in json.loads(hooks.read_text())['hooks'].values():
        command = entry[0]['hooks'][0]['command']
        assert (root / command.split('${PLUGIN_ROOT}/')[1].rstrip('"')).is_file()
