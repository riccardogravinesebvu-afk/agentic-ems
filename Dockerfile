# The demo server with recorded answers. Build: docker build -t agentic-ems:local .
# The image keeps the repository layout under /app: the server finds frontend/dist and data/ from its own source file.

FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.24@sha256:816fdce3387ed2142e37d2e56e1b1b97ccc1ea87731ba199dc8a25c04e4997c5 /uv /usr/local/bin/uv
# The image's own Python, so the virtual environment still works in the last stage.
ENV UV_PYTHON_DOWNLOADS=never UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src/ src/
RUN uv sync --locked --no-dev
RUN .venv/bin/python -m agentic_ems.generator

FROM python:3.14-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b
WORKDIR /app
COPY --from=build /app/.venv .venv
COPY --from=build /app/src src
COPY --from=build /app/data/plant.duckdb data/plant.duckdb
COPY --from=frontend /app/frontend/dist frontend/dist
# Outside a container the server binds to 127.0.0.1; here the port must be reachable from outside the container.
ENV PATH=/app/.venv/bin:$PATH PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEMO_HOST=0.0.0.0
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "agentic_ems.demo", "serve"]
