# ============================================================
# Open Manus — Multi-Agent Dockerfile
# Based on Hermes-Agent by Nous Research
# Each Railway service runs ONE agent, selected by AGENT_NAME
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
    libopus-dev \
    libpq-dev \
    poppler-utils \
    ffmpeg \
    bc \
    less \
    net-tools \
    socat \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js 20 (required for some tools)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python package management
RUN pip install uv

# Set working directory
WORKDIR /app

# Copy dependency files first for layer caching
COPY pyproject.toml requirements.txt ./

# Install Python dependencies (all extras for full functionality)
RUN uv pip install --system -e ".[all]" 2>/dev/null || pip install -e ".[all]" 2>/dev/null || pip install -r requirements.txt

# Install additional dependencies for extended tool capabilities
RUN pip install \
    python-pptx \
    markdown \
    weasyprint \
    pdf2image \
    pillow \
    elevenlabs \
    firecrawl-py \
    playwright \
    redis \
    stripe \
    python-dotenv \
    discord-ext-voice-recv \
    PyNaCl

# Install Playwright browsers for web automation
RUN playwright install chromium --with-deps || true

# Copy the entire application
COPY . .

# Install mini-swe-agent submodule if present
RUN if [ -f "mini-swe-agent/setup.py" ] || [ -f "mini-swe-agent/pyproject.toml" ]; then \
    pip install -e ./mini-swe-agent; \
    fi

# Install hermes-agent itself as a CLI tool
RUN pip install -e . 2>/dev/null || true

# Create workspace and config directories
RUN mkdir -p /root/.hermes/workspace /root/.hermes/skills /root/.hermes/memory /root/.hermes/sessions

# Make entrypoint executable
RUN chmod +x /app/entrypoint.sh

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV HERMES_WORKSPACE_DIR=/root/.hermes/workspace
ENV TERMINAL_ENV=local
ENV TERMINAL_TIMEOUT=180

# Default agent (overridden per Railway service)
ENV AGENT_NAME=harmony

# Start via the multi-agent entrypoint
CMD ["/app/entrypoint.sh"]
