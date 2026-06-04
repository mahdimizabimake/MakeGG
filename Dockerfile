FROM python:3.11-slim

WORKDIR /app

# نصب وابستگی‌های سیستمی مورد نیاز pytgcalls و ffmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    gcc \
    g++ \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# کپی فایل requirements و نصب کتابخانه‌های پایتون
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# کپی کل کد
COPY . .

# اجرای ربات
CMD ["python", "main.py"]
