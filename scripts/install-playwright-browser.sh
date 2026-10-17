#!/bin/bash
# Install Playwright Chromium browser at build time.
# Uses --force to ensure a COMPLETE fresh install (stale/corrupt cached
# browsers caused "Executable doesn't exist" failures).
set -u
echo "=== Installing Playwright Chromium (force) ==="
python3 -m playwright install --force chromium 2>&1 | tail -8
echo "=== Verifying install ==="
ls -la /opt/render/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null || ls -la ~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome 2>/dev/null || echo "WARNING: chrome binary not found after install"
exit 0
