# syntax=docker/dockerfile:1
FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN addgroup --system app && adduser --system --ingroup app app

COPY pyproject.toml README.md ./
COPY genai_traces ./genai_traces
RUN pip install --upgrade pip && pip install ".[postgres,demo]"

RUN mkdir -p /data && chown app:app /data
USER app
ENV DATABASE_URL=sqlite:////data/traces.db
EXPOSE 4318

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"4318\")}/health').status == 200 else 1)"

CMD ["sh", "-c", "uvicorn genai_traces.asgi:app --host 0.0.0.0 --port ${PORT:-4318}"]
