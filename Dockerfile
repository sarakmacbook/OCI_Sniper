FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# OCI's published wheels avoid a compiler toolchain at runtime. Keeping the
# image free of apt build packages saves memory, disk and build time.
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY gunicorn.conf.py app.py ./
COPY templates ./templates

# Do not run the web process as root on a VPS or Railway container.
RUN addgroup --system app && adduser --system --ingroup app app \
    && chown -R app:app /app
USER app

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '5000'), timeout=3)"

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
