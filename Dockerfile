# ---- Base ----
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Streamlit runtime
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0

WORKDIR /app

# System deps (minimal; Playwright --with-deps will add the rest)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install Chromium for Playwright (so "Use computed-style contrast" works)
RUN python -m playwright install --with-deps chromium

# App
COPY app.py ./app.py

# Data dir for exports/logs (mount a volume here in compose)
RUN mkdir -p /app/data

EXPOSE 8501

# Optional: run as non-root
RUN useradd -ms /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
