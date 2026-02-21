# ─────────────────────────────────────────────
#  MCP Inbox — Dockerfile
# ─────────────────────────────────────────────
FROM python:3.12-slim

# System deps (needed by some google libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (credentials/tokens are excluded via .dockerignore)
COPY . .

# Data directory for SQLite DB and any mounted credential files
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "ui.server:app", "--host", "0.0.0.0", "--port", "8000"]
