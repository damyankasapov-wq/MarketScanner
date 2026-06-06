# MarketScanner — headless scanner container
#
# Uses python:3.11-slim (Debian-based) so apt-get works and psycopg2-binary
# compiles cleanly.  matplotlib runs in Agg (non-interactive) mode via HEADLESS=1.
#
# Build:   docker build -t marketscanner .
# Run:     docker compose up -d   (preferred — handles DB dependency)

FROM python:3.11-slim

# Minimal system deps:
#   gcc / libpq-dev  — required by psycopg2-binary's C extension
#   libfreetype6     — required by matplotlib/mplfinance for font rendering
#   libpng-dev       — required by matplotlib PNG output
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        libfreetype6 \
        libpng-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps before copying source so Docker layer-caches them
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Ensure output dirs exist (mapped to a volume in docker-compose)
RUN mkdir -p output/backtest tmp

# Non-interactive matplotlib backend — no DISPLAY required
ENV HEADLESS=1

CMD ["python", "main.py"]
