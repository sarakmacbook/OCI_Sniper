FROM python:3.12-slim

WORKDIR /app

# gcc/libffi for oci SDK; tzdata so zoneinfo works (slim images ship without it)
RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev tzdata && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir tzdata

COPY . .

ENV PYTHONUNBUFFERED=1 \
    KEEP_ALIVE=true \
    TZ=Asia/Phnom_Penh

# Use shell form so $PORT is expanded at runtime
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 120
