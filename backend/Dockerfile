FROM python:3.11-slim

WORKDIR /app

# 系统依赖：OpenCV/rapidocr 运行所需 + gcc
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 gcc curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY . .

EXPOSE 8001
# WORKDIR=/app，import app.main 即可；配置优先读环境变量（compose env_file 注入）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
