"""Small, predictable Gunicorn configuration for Railway and tiny VPS hosts."""
import os

bind = "0.0.0.0:{0}".format(os.environ.get("PORT", "5000"))
workers = 1
worker_class = "gthread"
threads = 2

# OCI calls can take a while when a region is busy. The provisioning loop is
# already in a background thread, so this only protects normal HTTP requests.
timeout = 120
graceful_timeout = 30
keepalive = 5

# Avoid writing worker temp files to the container filesystem when possible.
worker_tmp_dir = "/dev/shm"

# Keep logs useful without enabling access logging for every polling request.
accesslog = None
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
preload_app = False
