ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE} AS dependencies
WORKDIR /build
COPY requirements.lock ./
# 普通 COPY 兼容服务器旧构建器；缓存 wheel 只留在构建阶段，不进入运行镜像。
COPY .wheels /wheels
ARG PIP_NO_INDEX=0
RUN pip install --no-cache-dir --ignore-installed --find-links=/wheels --prefix=/install -r requirements.lock

FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY --from=dependencies /install /usr/local
COPY app ./app
COPY scripts ./scripts
COPY migrations ./migrations
# 在构建阶段确认配置模块进入镜像；只导入定义，不加载私有 .env 或连接数据库。
RUN python -c "from app.config.settings import Settings" \
    && useradd --uid 10001 --create-home collector && mkdir /app/state && chown collector /app/state
USER collector
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
