FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . && useradd --create-home --uid 1000 app && mkdir -p /app/data && chown app /app/data

USER app
# Database, uploaded files and the inbox folder live here: mount a volume to keep them.
VOLUME ["/app/data"]
ENV HOST=0.0.0.0 PORT=8097 DATA_DIR=/app/data
EXPOSE 8097
CMD ["doc-intake", "serve"]
