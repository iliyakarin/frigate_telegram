# Stage 1: Builder
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: Final Image
FROM python:3.11-slim
LABEL maintainer="Iliya Karin"
LABEL org.opencontainers.image.description="Frigate NVR event notifications for Telegram"

# Non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Copy only the installed site-packages from the builder
COPY --from=builder /install /usr/local
COPY main.py .

# Create data dir with correct ownership
RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys; sys.exit(0)"

CMD ["python", "-u", "main.py"]