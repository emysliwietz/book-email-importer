FROM python:3.13-slim

# calibre (ebook-meta / ebook-convert) for metadata, covers and MOBI/AZW3/FB2 -> EPUB; poppler for PDF text
RUN apt-get update && \
    apt-get install -y --no-install-recommends calibre poppler-utils ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN pip install --no-cache-dir \
    "imapclient>=3.1.0,<4.0.0" \
    "python-dotenv>=1.0.0" \
    "requests>=2.32,<3" \
    "pillow>=11,<13"

COPY src/ ./src/
# HOME/CALIBRE_CONFIG_DIRECTORY: calibre needs a writable home, also when the container runs as a non-root user
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox" \
    HOME=/tmp CALIBRE_CONFIG_DIRECTORY=/tmp/calibre-config \
    STATE_FILE=/data/last_seen_uid.txt WORK_DIR=/data/work LIBRARY_DIR=/books/ebooks
VOLUME ["/data"]

CMD ["python", "-m", "book_importer.main"]
