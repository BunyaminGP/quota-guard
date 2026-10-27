#!/usr/bin/env bash
# install.sh — standalone installer for quota-guard's Antigravity part
# (hooks + the agy-run launcher; Unix / macOS / Linux / Git Bash). Mirrors the
# repo layout, so the root hooks.json paths (./agy/scripts/...) and
# ../common/resume_core.py work as-is.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # <repo>/agy
REPO_DIR="$(cd "$SRC_DIR/.." && pwd)"
TARGET_DIR="${AGY_PLUGIN_INSTALL_DIR:-$HOME/.agents/plugins/quota-guard}"
BIN_DIR="${AGY_BIN_INSTALL_DIR:-$HOME/.local/bin}"

echo "Installing quota-guard (Antigravity) to $TARGET_DIR ..."
mkdir -p "$TARGET_DIR/.antigravity-plugin" "$TARGET_DIR/common" "$BIN_DIR"
mkdir -p "$TARGET_DIR/agy/scripts" "$TARGET_DIR/agy/bin" "$TARGET_DIR/agy/locales" "$TARGET_DIR/agy/templates" "$TARGET_DIR/agy/shell"
TARGET_ABS="$(cd "$TARGET_DIR" && pwd)"
AGY_ABS="$TARGET_ABS/agy"

cp "$REPO_DIR/.antigravity-plugin/plugin.json" "$TARGET_ABS/.antigravity-plugin/plugin.json"
cp "$REPO_DIR/hooks.json" "$TARGET_ABS/hooks.json"
cp "$REPO_DIR/common/resume_core.py" "$TARGET_ABS/common/resume_core.py"
cp "$SRC_DIR/scripts/"*.py "$AGY_ABS/scripts/"
cp "$SRC_DIR/bin/agy-run" "$AGY_ABS/bin/agy-run"
cp "$SRC_DIR/bin/agy-run.cmd" "$AGY_ABS/bin/agy-run.cmd"
cp "$SRC_DIR/locales/"*.json "$AGY_ABS/locales/"
cp "$SRC_DIR/templates/progress.md" "$AGY_ABS/templates/progress.md"
cp "$SRC_DIR/shell/"* "$AGY_ABS/shell/"

chmod +x "$AGY_ABS/bin/agy-run" "$AGY_ABS/scripts/"*.py

cat > "$BIN_DIR/agy-run" <<EOF
#!/usr/bin/env bash
export AGY_QUOTA_GUARD_HOME="\${AGY_QUOTA_GUARD_HOME:-$AGY_ABS}"
exec "\$AGY_QUOTA_GUARD_HOME/bin/agy-run" "\$@"
EOF
chmod +x "$BIN_DIR/agy-run"

cat > "$BIN_DIR/agy-run.cmd" <<EOF
@echo off
setlocal
if not defined AGY_QUOTA_GUARD_HOME set "AGY_QUOTA_GUARD_HOME=$AGY_ABS"
call "%AGY_QUOTA_GUARD_HOME%\bin\agy-run.cmd" %*
exit /b %ERRORLEVEL%
EOF

echo "Installed plugin to: $TARGET_ABS"
echo "Installed shim to:   $BIN_DIR/agy-run"
