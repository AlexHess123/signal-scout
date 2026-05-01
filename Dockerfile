FROM python:3.12-slim

WORKDIR /app

COPY bot.py /app/bot.py
COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r /app/requirements.txt

CMD ["python", "bot.py", "--mode", "live"]
