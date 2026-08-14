FROM python:3.11-slim

WORKDIR /app

# 复制依赖文件（注意路径前缀 render-app/）
COPY render-app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制整个应用代码
COPY render-app/ .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]