FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install -r requirements.txt
COPY LICENSE NOTICE ./
COPY src ./src

RUN useradd --create-home --uid 10001 --user-group appuser \
    && chown -R appuser:appuser /app
USER 10001:10001

EXPOSE 8000
CMD ["python", "-m", "aerodoppler"]
