FROM python:3.11-slim

# ติดตั้ง ffmpeg (สำคัญที่สุด)
RUN apt-get update && apt-get install -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:10000", "--timeout", "600"]
