# Render free tier: Track A paper-trading backend (supervisor + engine).
# Python 3.13 + Node 24 (the engine shells out to db.mjs / @libsql/client).
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY automation/paper-trading/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY automation/paper-trading/ ./automation/paper-trading/
WORKDIR /app/automation/paper-trading
CMD ["python", "render_app.py"]
