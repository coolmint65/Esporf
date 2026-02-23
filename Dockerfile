FROM python:3.12-slim AS base

RUN groupadd -r esporf && useradd -r -g esporf esporf

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . 2>/dev/null || true

COPY . .
RUN pip install --no-cache-dir -e .

RUN mkdir -p /app/data && chown -R esporf:esporf /app/data

USER esporf

VOLUME ["/app/data"]

ENTRYPOINT ["esporf"]
CMD ["run"]
