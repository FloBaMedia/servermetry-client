FROM python:3.12-slim-bookworm

# iproute2: host interface addresses via `ip addr` (linux collector).
RUN apt-get update \
    && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/servermetry
COPY agent/ /opt/servermetry/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SERVERMETRY_CONTAINER=1 \
    SERVERMETRY_HOST_ROOT=/host

# Host metrics require /host (see docker-compose.yml). --check does not need an API key.
HEALTHCHECK --interval=120s --timeout=30s --start-period=20s --retries=3 \
    CMD python3 /opt/servermetry/agent.py --check || exit 1

CMD ["python3", "/opt/servermetry/agent.py", "--loop"]
