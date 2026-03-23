# BMS Watchlist — Dockerfile
# Uses the official Playwright image — Chromium + all deps pre-installed

FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# Shell form so $PORT is expanded by Railway at runtime
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
