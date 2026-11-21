FROM debian:bookworm-slim
ARG KASMVNC_VERSION=1.5.2
ARG KASMVNC_DEB=kasmvncserver_debian_bookworm_1.5.2_amd64.deb
USER root
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg python3 python3-venv ffmpeg supervisor \
        xvfb openbox x11-utils x11-xserver-utils fonts-noto-cjk fonts-liberation procps \
        libgbm1 libnss3 libatk-bridge2.0-0 libgtk-3-0 libasound2 libxss1 libxtst6 && \
    curl -fsSL "https://github.com/kasmtech/KasmVNC/releases/download/v${KASMVNC_VERSION}/${KASMVNC_DEB}" -o /tmp/kasmvnc.deb && \
    apt-get install -y --no-install-recommends /tmp/kasmvnc.deb && \
    rm -f /tmp/kasmvnc.deb && \
    curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/google-chrome-stable.deb && \
    apt-get install -y --no-install-recommends /tmp/google-chrome-stable.deb && \
    rm -f /tmp/google-chrome-stable.deb && \
    test -x /usr/bin/Xvnc && test -x /opt/google/chrome/chrome && \
    groupadd -g 1000 beagle && \
    useradd -m -u 1000 -g 1000 -s /bin/bash beagle && \
    mkdir -p /data/google-flow /run/user/1000 /app && \
    chown -R 1000:1000 /data/google-flow /run/user/1000 /app && \
    rm -rf /var/lib/apt/lists/*
ENV HOME=/home/beagle \
    USER=beagle \
    XDG_RUNTIME_DIR=/run/user/1000 \
    DISPLAY=:99 \
    GFLOW_CLI_TELEMETRY=0 \
    DO_NOT_TRACK=1 \
    GFLOW_CLI_UPDATE_CHECK=false \
    GFLOW_CLI_INCIDENT_CAPTURE=false \
    GFLOW_CLI_HISTORY_PROMPTS=redacted \
    GFLOW_CLI_BROWSER_ENGINE=playwright \
    GFLOW_CLI_HOME=/data/google-flow/gflow \
    GFLOW_CLI_OUTPUT_DIR=/data/google-flow/output \
    GOOGLE_FLOW_STATE_DIR=/data/google-flow/state \
    GOOGLE_FLOW_DASHBOARD_DIR=/app/dashboard \
    GOOGLE_FLOW_DESKTOP_CONTROL=/app/runtime/desktop-control.py \
    GOOGLE_FLOW_NO_GPU=true \
    PATH="/opt/google-flow/bin:${PATH}"
COPY pyproject.toml ./
COPY google_flow_gateway/ google_flow_gateway/
COPY dashboard/ dashboard-source/
RUN python3 -m venv /opt/google-flow && \
    /opt/google-flow/bin/python -m pip install --no-cache-dir . && \
    /opt/google-flow/bin/python -m pip check && \
    /opt/google-flow/bin/python -c "from importlib.metadata import version; assert version('gflow-cli') == '0.103.1'" && \
    /opt/google-flow/bin/python -m google_flow_gateway.dashboard --source dashboard-source --output dashboard && \
    rm -rf dashboard-source && \
    chown -R 1000:1000 /app /opt/google-flow
COPY runtime/ runtime/
RUN chmod 755 runtime/entrypoint.sh runtime/desktop-control.py && \
    chown -R 1000:1000 /app/runtime && \
    test "$(id -u beagle)" = 1000
USER 1000:1000
EXPOSE 8000 8001 48083
ENTRYPOINT ["/app/runtime/entrypoint.sh"]
