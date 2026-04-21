# Stage 1: Build React frontend
FROM node:22-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Python runtime
FROM python:3.12-slim AS runtime
WORKDIR /app

# System deps for lxml, opencv, audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 libgl1 libglib2.0-0 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast Python dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project files and install Python deps
COPY pyproject.toml uv.lock ./
COPY itat_scraper/ itat_scraper/
COPY web/ web/
COPY main.py tui.py ./

RUN uv sync --no-dev --frozen

# Copy built frontend
COPY --from=frontend /app/frontend/dist frontend/dist/

# Downloads volume
RUN mkdir -p /app/downloads
VOLUME /app/downloads

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8000"]
