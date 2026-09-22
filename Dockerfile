FROM alpine:3.22 AS techdb
RUN apk add --no-cache git
RUN git clone --depth 1 --branch v0.4.5 https://github.com/rverton/webanalyze.git /src

FROM node:22-bookworm-slim AS lighthouse
RUN npm install -g lighthouse@13.5.0

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TECH_FINGERPRINTS_PATH=/opt/webanalyze/technologies.json \
    LIGHTHOUSE_BIN=/usr/local/bin/lighthouse

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=lighthouse /usr/local/bin/node /usr/local/bin/node
COPY --from=lighthouse /usr/local/lib/node_modules/lighthouse /opt/lighthouse
RUN ln -s /opt/lighthouse/cli/index.js /usr/local/bin/lighthouse

RUN mkdir -p /opt/webanalyze
COPY --from=techdb /src/technologies.json /opt/webanalyze/technologies.json
COPY --from=techdb /src/LICENSE /opt/webanalyze/LICENSE

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY . .
RUN mkdir -p /app/data/jobs /app/data/premium

CMD ["python", "main.py"]
