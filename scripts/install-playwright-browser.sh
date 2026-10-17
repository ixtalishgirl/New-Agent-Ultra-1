#!/bin/bash
# Install Playwright Chromium browser at build time.
# Called from the Render Build Command. Installs FULL chromium (not just
# headless-shell) because the agent daemon launches it with --headless=new.
# The `|| true` guards keep the build green if the install has issues;
# the agent also self-heals at runtime via ensure_browser.py.
set -u
python -m playwright install chromium || python -m playwright install --only-shell chromium || true
