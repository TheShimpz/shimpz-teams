# syntax=docker/dockerfile:1@sha256:87999aa3d42bdc6bea60565083ee17e86d1f3339802f543c0d03998580f9cb89
# A networkless SDK Assistant with one file-taking Action, for the opt-in Action file delivery proof (ADR-0093).
FROM python:3.14-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY wheel/ /tmp/wheel/
RUN python3 -m venv /opt/shimpz/runtime \
    && /opt/shimpz/runtime/bin/python3 -m pip install --no-index --no-cache-dir /tmp/wheel/*.whl \
    && rm -rf /tmp/wheel
COPY project/ /opt/shimpz/
RUN printf '%s\n' '#!/opt/shimpz/runtime/bin/python3.14' \
        'import sys' \
        'from shimpz._bridge import main' \
        'raise SystemExit(2 if len(sys.argv) != 2 else main(["invoke", "/opt/shimpz", sys.argv[1]]))' \
        > /usr/local/bin/shimpz-action \
    && chmod 0555 /usr/local/bin/shimpz-action \
    && find /opt/shimpz -path /opt/shimpz/runtime -prune -o -type d -exec chmod 0555 {} + \
    && find /opt/shimpz -path /opt/shimpz/runtime -prune -o -type f -exec chmod 0444 {} +

USER 10001:10001
ENTRYPOINT ["/usr/local/bin/python3", "-c", "import signal; signal.pause()"]
