# CI downloads dependencies online; no local .wheels cache is needed.
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
RUN useradd --uid 10001 --create-home collector && mkdir /app/state && chown collector /app/state
USER collector
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
