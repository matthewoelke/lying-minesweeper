# Lying Minesweeper - production image
# Stdlib-only Python; no pip install needed.

FROM python:3.13-slim

# Non-root runtime user
RUN groupadd --system --gid 10001 app \
 && useradd  --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# Copy the project. .dockerignore keeps junk out.
COPY --chown=app:app . /app

# Persistent SQLite lives outside the read-only app layer.
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]

ENV LMS_HOST=0.0.0.0 \
    LMS_PORT=8088 \
    LMS_DB_PATH=/data/leaderboard.db \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8088

USER app

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8088/api/health',timeout=3).status==200 else 1)" || exit 1

CMD ["python", "-m", "server.app"]
