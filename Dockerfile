# Vélo'v Control on the Raspberry Pi (arm64). Standard library only.
FROM python:3.12-slim
RUN pip install --no-cache-dir tzdata
WORKDIR /app
COPY app/ /app/
ENV VELOV_DATA=/data \
    VELOV_HOST=0.0.0.0 \
    VELOV_PORT=8765 \
    TZ=Europe/Paris \
    PYTHONUNBUFFERED=1
EXPOSE 8765
HEALTHCHECK --interval=2m --timeout=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/status', timeout=8)"
CMD ["python", "/app/velov_app.pyw"]
