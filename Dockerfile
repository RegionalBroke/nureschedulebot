FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt bot.py ./
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "-u", "bot.py"]
