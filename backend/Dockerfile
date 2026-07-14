FROM python:3.11-slim

WORKDIR /app

# 系统依赖：OpenCV/rapidocr 运行所需 + gcc
# 国内用阿里云 Debian 镜像加速
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true && \
    apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 gcc curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# 先装 CPU-only torch（避开 GPU/CUDA 几 GB；bge 走 CPU 推理够用），再装其余依赖
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu --trusted-host download.pytorch.org && \
    pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

COPY . .

EXPOSE 8001
# WORKDIR=/app，import app.main 即可；配置优先读环境变量（compose env_file 注入）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
