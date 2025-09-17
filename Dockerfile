# ── Dockerfile ────────────────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.55.0-jammy

# System locale (optional, nice for PDFs/text shaping)
ENV LANG=C.UTF-8
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Workdir
WORKDIR /app

# Copy reqs first for better caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Browsers already present in this base image, but keep this harmless no-op for safety:
RUN python -m playwright install --with-deps chromium

# App
COPY app.py /app/app.py
COPY .streamlit /app/.streamlit

# Streamlit settings (ports etc.)
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0

# Sensible defaults (you can override at deploy time)
ENV DATA_DIR=/app/data
ENV ENABLE_COMPUTED_CONTRAST=true
ENV RETENTION_DAYS=90
RUN mkdir -p ${DATA_DIR}

EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
