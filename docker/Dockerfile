# jouletrace/docker/Dockerfile
# Multi-stage build for optimized production image

# Build stage
FROM python:3.11-slim as builder

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy requirements and install Python dependencies
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /tmp/requirements.txt

# Production stage
FROM python:3.11-slim

# Install runtime dependencies including perf tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Perf tools for energy measurement (Debian package providing `perf`)
    linux-perf \
    # System utilities
    sudo \
    procps \
    # Cleanup
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Set up non-root user for security (but with sudo for perf)
RUN useradd -m -u 1000 -s /bin/bash jouletrace && \
    echo "jouletrace ALL=(ALL) NOPASSWD: /usr/bin/perf" >> /etc/sudoers

# Set working directory
WORKDIR /app

# Copy application code
COPY --chown=jouletrace:jouletrace . /app/

# Set environment variables
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    JOULETRACE_ENVIRONMENT=production

# Create necessary directories
RUN mkdir -p /var/log/jouletrace && \
    chown jouletrace:jouletrace /var/log/jouletrace

# Switch to non-root user
USER jouletrace

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/ping').raise_for_status()"

# Default command (can be overridden)
CMD ["python", "-m", "uvicorn", "jouletrace.api.service:app", "--host", "0.0.0.0", "--port", "8000"]
