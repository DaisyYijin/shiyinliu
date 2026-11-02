# 拾音流：抖音视频 → 音频 → 喜马拉雅
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

# ffmpeg（音频转码）+ 时区数据
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 依赖层（利用构建缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache/pip

# 应用代码
COPY app ./app
COPY static ./static

EXPOSE 8300
VOLUME ["/app/config", "/app/data", "/app/media"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8300/api/health',timeout=5)" || exit 1

CMD ["python", "-m", "app"]
