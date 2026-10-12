FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MANAGER_URL=http://manager:8000 \
    PROXY_PORT=8888 \
    ADMIN_PORT=8000 \
    EGRESS_HOST=127.0.0.1 \
    REGION=local \
    LOG_LEVEL=INFO

WORKDIR /app

# 节点只用标准库，不需要 pip install
COPY proxy.py admin.py registrar.py entrypoint.py ./

EXPOSE 8888 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/ping', timeout=2).status == 200 else 1)"

ENTRYPOINT ["python", "-u", "entrypoint.py"]
