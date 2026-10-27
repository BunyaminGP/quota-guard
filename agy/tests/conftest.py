import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


@pytest.fixture(autouse=True)
def _isolated_user_settings(tmp_path_factory, monkeypatch):
    """Never read the real ~/.quota-guard/settings.json (its notify_url would
    send real pushes) or the real Antigravity conversation of a dev shell.
    Set in os.environ so gate subprocesses inherit it too."""
    missing = tmp_path_factory.mktemp("qg") / "no-shared-settings.json"
    monkeypatch.setenv("QUOTA_GUARD_CONFIG", str(missing))
    for key in ("ANTIGRAVITY_CONVERSATION_ID", "AGY_QUOTA_RUNNER", "AGY_CHAT_AUTO_RESUME",
                "AGY_PLUGIN_OPTION_CHAT_AUTO_RESUME"):
        monkeypatch.delenv(key, raising=False)
