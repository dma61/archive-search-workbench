# Archive Search Workbench — container image
# ===========================================
# Runs the Flask web app (port 5059) plus the system tools it shells out to.
#
# IMPORTANT: search over an existing catalog works in a plain container, but
# MOUNTING external drives, USB/IP attach and INDEXING need host-level
# privileges (privileged container + host /dev + host network + the vhci-hcd
# kernel module loaded ON THE HOST). See DOCKER.md for the two run modes.

FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# System tools the app invokes (mirrors setup.sh). unrar-free is used instead of
# the non-free unrar; RAR metadata support is best-effort.
RUN apt-get update && apt-get install -y --no-install-recommends \
        sudo ca-certificates \
        sqlite3 recoll \
        antiword catdoc poppler-utils \
        unzip p7zip-full unrar-free \
        ripgrep smartmontools libimage-exiftool-perl mediainfo \
        ntfs-3g udisks2 usbip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code (see .dockerignore for what is excluded).
COPY . .

# Runtime directories + the read-only ingest mountpoint.
RUN mkdir -p /app/data /app/recoll-indexes /app/logs /app/output /app/temp /mnt/archive-ingest

EXPOSE 5059

# Simple healthcheck against the web server.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5059/', timeout=4).status==200 else 1)" || exit 1

CMD ["python", "web_app.py"]
