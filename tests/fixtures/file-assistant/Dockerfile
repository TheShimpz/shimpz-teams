# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# A networkless SDK Assistant with one file-taking Action, for the opt-in Action file delivery proof (ADR-0093).
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY wheel/ /tmp/wheel/
RUN python3 -m venv /opt/shimpz/runtime \
    && /opt/shimpz/runtime/bin/python3 -m pip install --no-index --no-cache-dir /tmp/wheel/*.whl \
    && rm -rf /tmp/wheel
COPY project/ /opt/shimpz/
RUN printf '%s\n' '#!/opt/shimpz/runtime/bin/python3' \
        'import sys' \
        'from shimpz._bridge import main' \
        'raise SystemExit(2 if len(sys.argv) != 2 else main(["invoke", "/opt/shimpz", sys.argv[1]]))' \
        > /usr/local/bin/shimpz-action \
    && chmod 0555 /usr/local/bin/shimpz-action \
    && find /opt/shimpz -path /opt/shimpz/runtime -prune -o -type d -exec chmod 0555 {} + \
    && find /opt/shimpz -path /opt/shimpz/runtime -prune -o -type f -exec chmod 0444 {} +

USER 10001:10001
ENTRYPOINT ["/usr/local/bin/python3", "-c", "import signal; signal.pause()"]
