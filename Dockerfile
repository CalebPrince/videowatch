FROM python:3.14-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN python -m playwright install --with-deps || true

EXPOSE 8000

CMD ["python", "server.py"]
