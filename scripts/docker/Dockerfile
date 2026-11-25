# RoutePilot runtime image.
# No torch: graph v3 calls the JEV signal engine over HTTP (ROUTER_JEV_URL).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ROUTER_HOST=0.0.0.0 \
    ROUTER_PORT=8400 \
    ROUTER_DB=/data/routepilot.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY router/ ./router/
COPY configs/ ./configs/
COPY scripts/configure_upstream.py scripts/apply_default_routing_tree.py scripts/configure_163_routepilot.py ./scripts/

RUN groupadd -g 10001 routepilot \
 && useradd -u 10001 -g 10001 -s /usr/sbin/nologin -m routepilot \
 && mkdir -p /data \
 && chown -R routepilot:routepilot /app /data

USER 10001:10001

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8400/healthz', timeout=2).status == 200 else 1)"

EXPOSE 8400
ENTRYPOINT ["python", "-m", "router"]
