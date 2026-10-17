#!/bin/bash
# Install Playwright Chromium browser at build time.
# Called from the Render Build Command. Installs FULL chromium (not just
# headless-shell) because the agent daemon launches it with --headless=new.
set -u
echo "=== Installing Playwright Chromium ==="
echo "Python: $(which python3)"
python3 -m playwright install chromium 2>&1 | tail -5
if [ $? -ne 0 ]; then
  echo "Full chromium install failed, trying headless-shell..."
  python3 -m playwright install --only-shell chromium 2>&1 | tail -5
fi
echo "=== Browser install done ==="
echo "Contents of ms-playwright:"
ls -la /opt/render/.cache/ms-playwright/ 2>/dev/null || ls -la ~/.cache/ms-playwright/ 2>/dev/null || echo "No ms-playwright dir found"
# Always succeed so the build doesn't fail; the agent logs the real state.
exit 0
