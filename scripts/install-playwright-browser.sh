#!/bin/bash
# Install Playwright Chromium browser at build time.
# CRITICAL: Install to a path inside the project directory
# (/opt/render/project/src), NOT /opt/render/.cache (build cache is NOT
# persisted to the runtime image!). The runtime finds it via the
# PLAYWRIGHT_BROWSERS_PATH env var (set on the Render services).
set -u
export PLAYWRIGHT_BROWSERS_PATH="/opt/render/project/src/.playwright-browsers"
echo "=== Installing Playwright Chromium to $PLAYWRIGHT_BROWSERS_PATH ==="
mkdir -p "$PLAYWRIGHT_BROWSERS_PATH"
python3 -m playwright install --force chromium 2>&1 | tail -8
echo "=== Verifying install ==="
ls -la "$PLAYWRIGHT_BROWSERS_PATH/chromium-"/chrome-linux*/chrome 2>/dev/null && echo "OK: chrome binary present" || echo "WARNING: chrome binary not found"
exit 0
