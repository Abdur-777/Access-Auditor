FROM python:3.11-slim

# System deps for Playwright/Chromium & ReportLab fonts
RUN apt-get update && apt-get install -y \
    wget gnupg libnss3 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxi6 libxtst6 libglib2.0-0 libxrandr2 \
    libatk1.0-0 libatk-bridge2.0-0 libdrm2 libgbm1 libxfixes3 libpango-1.0-0 \
    libgtk-3-0 fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install chromium

COPY . .
ENV DATA_DIR=/var/app/data \
    PYTHONUNBUFFERED=1
RUN mkdir -p ${DATA_DIR}

EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
