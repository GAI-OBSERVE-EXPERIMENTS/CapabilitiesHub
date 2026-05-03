FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8007
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT:-8007}/health')" || exit 1
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8007}"]
