# syntax=docker/dockerfile:1
#
# Direct SD-card receiver.  The camera pushes media here over Tailscale (or
# the LAN); no inbound port forwarding is required.
#
#   docker build -f server/Dockerfile -t instabackup .
#   docker run --rm -p 8844:8844 \
#       -v "$PWD/appdata:/app:ro" -v "$PWD/data:/data" \
#       instabackup
#
# The token file is optional.  When it is present the receiver requires
# X-Auth-Token on every upload/stat/reset request.
FROM python:3.12-slim

ARG UID=99
ARG GID=100

# Reuse an existing group/user when the Debian base already has that id
# (gid 100 is "users" on Debian).  The numeric USER below is what matters.
RUN if ! getent group "${GID}" >/dev/null; then groupadd -g "${GID}" app; fi \
 && if ! getent passwd "${UID}" >/dev/null; then \
        useradd -u "${UID}" -g "${GID}" -M -s /usr/sbin/nologin app; \
    fi \
 && mkdir -p /data \
 && chown "${UID}:${GID}" /data

WORKDIR /app
COPY server/sdcard_receiver.py /app/sdcard_receiver.py
COPY server/docker-entrypoint.sh /usr/local/bin/instabackup-entrypoint

RUN chmod 0755 /usr/local/bin/instabackup-entrypoint

USER ${UID}:${GID}
ENV PYTHONUNBUFFERED=1
EXPOSE 8844
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/status' % os.environ.get('SDCARD_PORT','8844'), timeout=3).read(1)"

ENTRYPOINT ["/usr/local/bin/instabackup-entrypoint"]
