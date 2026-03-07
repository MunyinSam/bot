# Supports linux/amd64 and linux/arm64 (Raspberry Pi 4/5)
FROM python:3.11-slim

# Install ffmpeg and build tools for Python packages with C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    gcc \
    libffi-dev \
    libsodium-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py MyBot.py ./

# Data directory for the SQLite database
VOLUME ["/app/data"]

ENV FFMPEG_EXECUTABLE=ffmpeg

CMD ["python", "MyBot.py"]
