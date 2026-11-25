# RoutePilot runtime image: one build path, two deployable services.
#
# The container entry point is `python -m router <service>`:
#   * core       — decision kernel (graphs JSON + JEV decision + decision events)
#   * peripheral — console/SQLite/standalone model data plane
# Peripheral stays the default so existing single-service process managers that
# run plain `python -m router` keep starting the console service; compose and
# Kubernetes pass the service argument explicitly.
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

# /data/graphs is the kernel graph directory of the standalone compose form.
# Creating it before the named volume is mounted lets Docker copy uid 10001
# ownership into the volume; the console keeps its SQLite database in /data.
RUN groupadd -g 10001 routepilot \
 && useradd -u 10001 -g 10001 -s /usr/sbin/nologin -m routepilot \
 && mkdir -p /data/graphs \
 && chown -R routepilot:routepilot /app /data

USER 10001:10001

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8400/healthz', timeout=2).status == 200 else 1)"

EXPOSE 8400
ENTRYPOINT ["python", "-m", "router"]
# Default service when no argument is supplied (kept for backward compatibility).
CMD ["peripheral"]
