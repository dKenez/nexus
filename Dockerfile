# Dockerfile
# FROM ghcr.io/astral-sh/uv:latest
FROM ghcr.io/astral-sh/uv:python3.13-alpine

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
COPY uv.lock .
COPY README.md .
COPY .python-version .
COPY src ./src

RUN uv sync --frozen

# Expose port
EXPOSE 6789

# Run the application
CMD ["uv", "run", "fastapi", "run", "src/nexus/app/app.py", "--host", "0.0.0.0", "--port", "6789"]
