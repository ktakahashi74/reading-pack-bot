# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG TARGETPLATFORM=linux/amd64

FROM --platform=${TARGETPLATFORM} ${PYTHON_IMAGE} AS builder
WORKDIR /src
COPY requirements-build.lock requirements-live-linux-amd64.lock ./
RUN python -m pip download --only-binary=:all: --require-hashes \
      --requirement requirements-build.lock \
      --requirement requirements-live-linux-amd64.lock \
      --dest /wheels \
    && python -m pip install --no-cache-dir --no-index --find-links=/wheels \
      --require-hashes --requirement requirements-build.lock
COPY . .
RUN test ! -e build \
    && python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM --platform=${TARGETPLATFORM} ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.source="https://github.com/ktakahashi74/reading-pack-bot"
COPY --from=builder /wheels /wheels
COPY requirements-live-linux-amd64.lock /requirements-live-linux-amd64.lock
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels \
      --require-hashes --requirement /requirements-live-linux-amd64.lock \
    && python -m pip install --no-cache-dir --no-index --no-deps --find-links=/wheels \
      'reading-pack-bot==0.4.1' \
    && rm -rf /wheels \
    && rm -f /requirements-live-linux-amd64.lock \
    && mkdir -p /var/lib/reading-pack-bot \
    && chown 65532:65532 /var/lib/reading-pack-bot

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    READING_PACK_BOT_DISABLED=1
USER 65532:65532
ENTRYPOINT ["reading-pack-bot"]
CMD ["run", "--config", "/etc/reading-pack-bot/config.toml"]
