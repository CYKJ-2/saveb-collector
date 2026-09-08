FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock ./
ARG PIP_NO_INDEX=0
RUN --mount=type=bind,source=.wheels,target=/wheels pip install --no-cache-dir --find-links=/wheels -r requirements.lock
COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
RUN useradd --uid 10001 --create-home collector && mkdir /app/state && chown collector /app/state
USER collector
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

