# Contributing to quota-guard

Issues and pull requests are welcome. The repo holds one plugin for three
hosts; each host lives in its own folder with its own tests:

| Folder | Host | Tests |
|---|---|---|
| `claude/` | Claude Code | `python -m pytest -q`, `bash tests/test_cc_run.sh`, `python tests/check_bash32_compat.py bin/cc-run install.sh tests/test_cc_run.sh shell/cc-run.sh` |
| `codex/` | Codex CLI | `python -m pytest -q` |
| `agy/` | Antigravity CLI | `python -m pytest -q`, `agy plugin validate .. --strict` (needs `agy`) |
| `common/` | shared by all three | covered by the three suites |

Run the commands from inside each folder. CI (`.github/workflows/ci.yml`) runs
all of them on Ubuntu, macOS and Windows.

## Setup

Runtime code is Python standard library only. You need Python 3.10+, `git`,
`bash` (Git Bash on Windows) and `pytest` (`python -m pip install pytest`).

## Design principles

- **Fail open.** If usage can't be read, git is missing or a notification fails,
  never block the agent and never crash a hook.
- **The user's own settings only, for anything security-relevant.** Auto-revert
  (`git stash`), notification URLs and the chat auto-resume switch come from
  env vars, the host's plugin options or `~/.quota-guard/settings.json`, **never**
  from a project's `.cc-quota/`, `.codex-quota/` or `.agy-quota/` config, since
  those can arrive already committed in a cloned repo.
- **Never follow a planted path.** Files in a project's state folder are
  written with `mkstemp` + `os.replace` (see `common/resume_core.py`), and
  symlinked state files are ignored when read.
- **Credentials: read-only, minimal, never sent anywhere else.** Codex and
  Antigravity quota comes only from the CLI itself (Codex's
  `account/rateLimits/read` RPC, `agy -p /usage`); those parts never read token
  files. The Claude Code part reads Claude Code's own OAuth access token
  (`~/.claude/.credentials.json` or the macOS Keychain) only to call
  `api.anthropic.com/api/oauth/usage`, never writes it and never sends it
  anywhere else (see `claude/scripts/usage.py`).
- **Exact resumption.** Runners and wake-ups resume the exact session, thread
  or conversation ID, never "the last one".
- **Say what is verified.** If something is unverified (a host behavior, an
  OS you couldn't test), say so in the code comment and the README.

## Languages

User-facing messages exist in English and Turkish: `claude/locales/*.json`,
`agy/locales/*.json`, and `MESSAGES` in `codex/scripts/common.py`. A new message
needs both. Instructions to the agent stay in English.

## License

By contributing, you agree that your contribution is licensed under the
[MIT License](LICENSE).
