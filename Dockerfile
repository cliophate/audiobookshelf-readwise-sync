FROM python:3.13-slim
ARG BUILD_REVISION=source
LABEL org.opencontainers.image.source="https://github.com/cliophate/audiobookshelf-readwise-sync" \
      org.opencontainers.image.version="0.1.9" \
      org.opencontainers.image.revision="${BUILD_REVISION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    SYNC_BUILD_REVISION=${BUILD_REVISION}

WORKDIR /app
COPY pyproject.toml LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

USER 1000:1000
ENTRYPOINT ["abs-readwise-sync"]
