FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home disciple
COPY --chown=disciple:disciple . .
RUN mkdir -p /data && chown disciple:disciple /data
USER disciple
ENV DATA_DIR=/data COOKIE_SECURE=1
EXPOSE 8000
CMD ["sh", "-c", "flask --app app init-db && gunicorn --bind 0.0.0.0:8000 --workers 2 --access-logfile - app:app"]
