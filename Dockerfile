FROM python:3.12-slim AS builder
RUN pip install --no-cache-dir 'uv>=0.4,<1'
WORKDIR /app
COPY requirements.lock ./
RUN uv pip install --system --no-cache-dir -r requirements.lock

FROM python:3.12-slim
WORKDIR /app

# ENV до compileall, чтобы .pyc собирался с PYTHONOPTIMIZE=2 (без docstring'ов).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONOPTIMIZE=2 \
    MALLOC_ARENA_MAX=2

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY src ./src
COPY schema.sql ./
# Раздельный COPY: иначе содержимое migrations/ распыляется в /app/, а сама папка теряется
COPY migrations ./migrations

RUN python -m compileall src

# HEALTHCHECK — Coolify/Docker используют для restart-логики (ARCHITECTURE.md §7.6)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
                   r=urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3); \
                   sys.exit(0 if r.status==200 else 1)" || exit 1

CMD ["python", "-m", "src.main"]
