"""Optional ntfy-compatible status messages; never reads a project webhook URL
(env var, or the per-user ~/.quota-guard/settings.json shared by all hosts)."""
import os
import urllib.request
import urllib.error


def notify(text, title='codex-quota-guard'):
    url = os.environ.get('CODEX_QUOTA_NOTIFY_URL', '')
    if not url:
        try:
            import common  # noqa: F401  (puts the shared module on sys.path)
            import resume_core
            url = str(resume_core.user_config().get('notify_url') or '')
        except Exception:
            url = ''
    if not url.startswith(('https://', 'http://')):
        return False
    try:
        request = urllib.request.Request(url, data=text.encode('utf-8'),
                                         headers={'Title': title,
                                                  'Content-Type': 'text/plain; charset=utf-8'})
        with urllib.request.urlopen(request, timeout=5):
            return True
    except (OSError, ValueError, urllib.error.URLError):
        return False
