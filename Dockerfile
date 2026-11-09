# =============================================================================
# ReelsBot Production Dockerfile
# =============================================================================

FROM python:3.11-slim

# Install system dependencies (ffmpeg and build essentials)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency definition
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . .

# Ensure scripts are executable and directories exist
RUN chmod +x start_bot.sh reels_bot.py && \
    mkdir -p downloads

# Run bot as non-root user for security
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

CMD ["python3", "reels_bot.py"]

