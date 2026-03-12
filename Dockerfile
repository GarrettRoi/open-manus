# ============================================================
# Open Manus — Dockerfile
# Based on Hermes-Agent by Nous Research
# Configured for Railway deployment
# ============================================================

FROM python:3.11-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    wget \
    unzip \
    build-essential \
    libssl-dev \
    libffi-dev \
    libpq-dev \
    poppler-utils \
    ffmpeg \
    nodejs \
    npm \
    bc \
    less \
    net-tools \
    socat \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python package management
RUN pip install uv

# Set working directory
WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml requirements.txt ./

# Install Python dependencies
RUN uv pip install --system -e ".[all]" || pip install -r requirements.txt

# Install additional Open Manus dependencies
RUN pip install \
    python-pptx \
    markdown \
    weasyprint \
    pdf2image \
    pillow \
    elevenlabs \
    firecrawl-py \
    playwright \
    redis

# Install Playwright browsers
RUN playwright install chromium --with-deps || true

# Copy the entire application
COPY . .

# Install mini-swe-agent submodule if present
RUN if [ -f "mini-swe-agent/setup.py" ] || [ -f "mini-swe-agent/pyproject.toml" ]; then \
    pip install -e ./mini-swe-agent; \
    fi

# Create workspace and config directories
RUN mkdir -p /root/.hermes/workspace /root/.hermes/skills /root/.hermes/memory

# Copy Open Manus skills into the hermes skills directory
RUN cp -r /app/skills/task-planner /root/.hermes/skills/ 2>/dev/null || true

# Copy the Open Manus config template
COPY open_manus_config.yaml /root/.hermes/config.yaml

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV HERMES_WORKSPACE_DIR=/root/.hermes/workspace
ENV TERMINAL_ENV=local
ENV TERMINAL_TIMEOUT=120
ENV PORT=8080

# Expose the gateway port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Start the Open Manus server
CMD ["python", "open_manus_server.py"]
