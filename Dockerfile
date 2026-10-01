FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DB_PATH=/srv/data/posts.db MEDIA_DIR=/srv/data/media
VOLUME /srv/data
EXPOSE 8000
CMD ["python", "-m", "app.main"]
