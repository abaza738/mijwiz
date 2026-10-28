FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home bot \
    && mkdir /opt/venv \
    && chown bot:bot /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

# venv owned by the non-root user so the startup yt-dlp upgrade can write to it
USER bot
RUN python -m venv /opt/venv
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py test_bot.py ./

# Upgrade yt-dlp on each start so a restart picks up YouTube fixes; keeps the bundled one if offline.
CMD ["sh", "-c", "pip install --no-cache-dir --disable-pip-version-check -q -U 'yt-dlp[default,deno]' || echo 'yt-dlp upgrade failed, using bundled version'; exec python bot.py"]
