FROM python:3.12-slim

WORKDIR /app

# Install build deps for oci SDK
RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1

# Use shell form so $PORT is expanded at runtime
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-5000} --workers 2 --timeout 120
