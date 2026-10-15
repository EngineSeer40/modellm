FROM ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 AS uv
FROM python:3.13.7-slim-bookworm@sha256:adafcc17694d715c905b4c7bebd96907a1fd5cf183395f0ebc4d3428bd22d92d
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" HOME=/tmp/modellm MODELLM_RUNS_ROOT=/data/runs
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache
COPY app.py ./
COPY src/ ./src/
COPY .streamlit/config.toml ./.streamlit/config.toml
COPY samples/ ./samples/
RUN uv sync --frozen --no-dev --no-editable --no-cache \
    && groupadd --gid 10001 modellm \
    && useradd --uid 10001 --gid 10001 --no-create-home modellm \
    && mkdir -p /data /tmp/modellm \
    && chown 10001:10001 /data /tmp/modellm
ARG GIT_SHA=unknown
ARG APP_VERSION=0.1.0
ENV MODELLM_GIT_SHA=$GIT_SHA MODELLM_APP_VERSION=$APP_VERSION
USER 10001:10001
VOLUME ["/data", "/tmp"]
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"]
CMD ["python", "-m", "streamlit", "run", "app.py"]
