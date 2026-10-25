"""Gunicorn configuration for the production web service (Stage 8.1).

Usage (after the gunicorn dependency is approved and installed, see
docs/deployment.md section 0):

    gunicorn config.wsgi -c deploy/gunicorn.conf.py

The PaaS (Railway/Render) provides the listen port via ``PORT``; workers,
timeouts and log routing stay overridable through environment variables so
the same image serves local drills and the production service.  Sync
workers fit the MVP profile: short page/HTMX requests, no in-process
background work (cron commands are separate short-lived services).
"""

import os

bind = f"0.0.0.0:{os.getenv('PORT', '8000')}"
workers = int(os.getenv("GUNICORN_WEB_WORKERS", "2"))
worker_class = "sync"
timeout = int(os.getenv("GUNICORN_TIMEOUT_SECONDS", "60"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT_SECONDS", "30"))
keepalive = 5
# Logs go to stdout/stderr so the PaaS log viewer collects them.
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(M)sms'
