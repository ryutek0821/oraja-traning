FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app
RUN groupadd --system processor \
    && useradd --system --gid processor --home-dir /app --no-create-home processor

# The build context is the repository root.  Keeping the source package in
# the image makes the Container use the same public core API as the local
# adapter instead of maintaining a second implementation.
COPY src /app/src
COPY cloudflare/container/health.py /app/container/health.py
COPY cloudflare/container/entrypoint.py /app/container/entrypoint.py
RUN chown -R processor:processor /app

USER processor

EXPOSE 8080
ENTRYPOINT ["python", "/app/container/entrypoint.py"]
