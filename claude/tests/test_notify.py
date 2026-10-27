"""
Tests for scripts/notify.py — the optional push-notification helper for
cc-quota-guard's key moments (threshold hit, resumed, done, gave up).

The positive path (an actual POST reaching a real endpoint) was also
verified manually against a real ntfy.sh topic + phone during development;
these tests cover it automatically with a local capturing HTTP server, plus
the fail-open guarantees that matter most for a feature that must never be
able to break the tool it's attached to.
"""

import http.server
import threading

import pytest

import notify


class _CapturingHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.captured.append({
            "path": self.path,
            "body": body.decode("utf-8"),
            "headers": dict(self.headers),
        })
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # keep test output quiet


@pytest.fixture
def capturing_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _CapturingHandler)
    server.captured = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)


# --------------------------------------------------------------------------- positive path (real POST, local server)

def test_notify_posts_message_body_and_title(capturing_server):
    port = capturing_server.server_address[1]
    url = f"http://127.0.0.1:{port}/some-topic"
    ok = notify.notify("quota threshold reached at 86%", title="cc-quota-guard", url=url)
    assert ok is True
    assert len(capturing_server.captured) == 1
    req = capturing_server.captured[0]
    assert req["body"] == "quota threshold reached at 86%"
    assert req["headers"]["Title"] == "cc-quota-guard"


def test_notify_body_is_utf8(capturing_server):
    """Message bodies (unlike the Title header) carry real UTF-8 text —
    this project ships a Turkish locale, so notification bodies can contain
    non-ASCII characters via cc-run's own already-localized status lines."""
    port = capturing_server.server_address[1]
    url = f"http://127.0.0.1:{port}/some-topic"
    message = "eşik %86'da tetiklendi, saat 14:00'te devam edilecek"
    assert notify.notify(message, url=url) is True
    assert capturing_server.captured[0]["body"] == message


def test_notify_title_is_optional(capturing_server):
    port = capturing_server.server_address[1]
    url = f"http://127.0.0.1:{port}/x"
    assert notify.notify("no title here", url=url) is True
    assert "Title" not in capturing_server.captured[0]["headers"]


# --------------------------------------------------------------------------- fail-open

def test_notify_fails_open_on_unreachable_url():
    assert notify.notify("should not raise", url="http://127.0.0.1:1/nowhere") is False


def test_notify_fails_open_with_no_url_configured(monkeypatch):
    monkeypatch.delenv("CC_NOTIFY_URL", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_NOTIFY_URL", raising=False)
    assert notify.notify("no url anywhere") is False


def test_notify_fails_open_on_malformed_url():
    assert notify.notify("garbage", url="not a url at all") is False


# --------------------------------------------------------------------------- _resolve_url precedence

def test_resolve_url_prefers_cc_notify_url_env(monkeypatch):
    monkeypatch.setenv("CC_NOTIFY_URL", "https://ntfy.sh/from-env")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_NOTIFY_URL", "https://ntfy.sh/from-plugin")
    assert notify._resolve_url() == "https://ntfy.sh/from-env"


def test_resolve_url_falls_back_to_plugin_option(monkeypatch):
    monkeypatch.delenv("CC_NOTIFY_URL", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_NOTIFY_URL", "https://ntfy.sh/from-plugin")
    assert notify._resolve_url() == "https://ntfy.sh/from-plugin"


def test_resolve_url_none_when_nothing_set(monkeypatch):
    monkeypatch.delenv("CC_NOTIFY_URL", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_NOTIFY_URL", raising=False)
    assert notify._resolve_url() is None


def test_notify_url_never_read_from_config_json(monkeypatch, tmp_path):
    """Regression guard: unlike the percentage thresholds, a notify URL is
    an exfiltration channel (session status sent to whoever controls that
    URL) rather than a cosmetic setting — .cc-quota/config.json can arrive
    already committed in a cloned/untrusted repo, so it must never be able
    to set this. Behavioral rather than a source-text search (the
    docstring itself mentions "config.json" in prose, which a naive
    substring check would trip on): tracks every file this module opens
    while resolving a URL and sending a notification, and asserts none of
    them is a config.json anywhere, mirroring how test_quota_gate.py
    guards hard_abort_enabled the same way."""
    opened_paths = []
    real_open = open

    def tracking_open(path, *args, **kwargs):
        opened_paths.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", tracking_open)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CC_NOTIFY_URL", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_NOTIFY_URL", raising=False)

    notify._resolve_url()
    notify.notify("test message", url=None)

    assert not any("config.json" in p for p in opened_paths)
