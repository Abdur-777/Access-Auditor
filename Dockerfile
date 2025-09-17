# Dockerfile
FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# (Optional) system packages you may want anyway
RUN apt-get update && apt-get install -y \
    wget gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libasound2 libpangocairo-1.0-0 libxshmfence1 libpango-1.0-0 \
    libcairo2 libatspi2.0-0 fonts-liberation \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# ⬇️ This is the line you asked about — put it right here
RUN python -m playwright install --with-deps chromium

COPY . .
ENV PORT=8501 STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
EXPOSE 8501
CMD ["bash","-lc","streamlit run app.py --server.address 0.0.0.0 --server.port ${PORT:-8501}"]
