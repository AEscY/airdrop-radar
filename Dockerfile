FROM python:3.11-slim

WORKDIR /app

# 直接安装所有依赖（不用 requirements.txt）
RUN pip install --no-cache-dir \
    fastapi==0.109.0 \
    uvicorn[standard]==0.27.0 \
    httpx==0.26.0 \
    parsel==1.8.1 \
    python-telegram-bot==20.6 \
    SQLAlchemy==2.0.25 \
    aiosqlite==0.19.0 \
    pydantic==2.5.0 \
    python-dotenv==1.0.0

# 复制应用代码
COPY render-app/ .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]