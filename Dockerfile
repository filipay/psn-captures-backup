FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

FROM base AS build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-compile .

FROM base AS runtime
LABEL org.opencontainers.image.source="https://github.com/filipay/psn-captures-backup"
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /captures \
    && chown appuser:appuser /captures
COPY --from=build /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=build /usr/local/bin/psn-captures-backup /usr/local/bin/psn-captures-backup
USER appuser
VOLUME ["/captures"]
ENTRYPOINT ["psn-captures-backup"]
CMD ["daemon"]
