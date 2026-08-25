FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py store.py memory.py schedule.py report.py /app/

# Run as uid 1000 so the bind-mounted /data git repo is owned by the NAS user.
# HOME must be writable for `git config --global safe.directory`.
RUN useradd -u 1000 -m app
ENV HOME=/home/app PYTHONUNBUFFERED=1
USER app

EXPOSE 8932
HEALTHCHECK --interval=60s --timeout=15s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8932/health', timeout=10)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8932"]
