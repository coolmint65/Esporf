# Stage 1: Build the React frontend
FROM node:20-slim AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# Stage 2: Python application
FROM python:3.12-slim AS base

RUN groupadd -r esporf && useradd -r -g esporf esporf

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . 2>/dev/null || true

COPY . .
RUN pip install --no-cache-dir -e .

# Copy the built frontend into the expected location
COPY --from=frontend-build /app/frontend/dist /app/frontend/dist

RUN mkdir -p /app/data && chown -R esporf:esporf /app/data

USER esporf

VOLUME ["/app/data"]

STOPSIGNAL SIGINT

ENTRYPOINT ["esporf"]
CMD ["run"]
