FROM python:3.11-slim

LABEL maintainer="frigate-telegram"
LABEL description="Frigate event notifications for Telegram"

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY main.py .

# Create data directory for persistent state
RUN mkdir -p /app/data

# Run unbuffered for real-time Docker logs
CMD ["python", "-u", "main.py"]
