# Browser service for ahsan's AI agent
# Build:  docker build -t ahsan-browser-service .
# Run:    docker run -d --name browser-service -p 8080:8080 \
#           --env-file .env -v browser-downloads:/app/downloads \
#           ahsan-browser-service
#
# RAM note: Chromium needs ~300-600MB per active tab in headless mode.
# Give the container at least 2GB for comfortable multi-tab research.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Playwright system dependencies for Chromium (Debian slim)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install Chromium + OS deps (this is the big layer, ~400-500MB)
RUN playwright install --with-deps chromium

COPY . .

# Non-root user for safety
RUN useradd -m -u 10001 browser && \
    mkdir -p /app/downloads && chown -R browser:browser /app
USER browser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
