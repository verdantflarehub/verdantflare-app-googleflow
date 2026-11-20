ARG REGISTRY_ENDPOINT_ALIYUN
FROM ${REGISTRY_ENDPOINT_ALIYUN}/wod/beagle-wind-vnc:1.2.0@sha256:0798799828a2a9b1e904a4e61c792d2b3278cdacff884870d51ea456f10c2cda
USER root
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends python3-venv ffmpeg && \
    if [ ! -x /opt/google/chrome/chrome ]; then \
        curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/google-chrome-stable.deb && \
        apt-get install -y --reinstall /tmp/google-chrome-stable.deb && rm /tmp/google-chrome-stable.deb; \
    fi && test -x /opt/google/chrome/chrome && rm -rf /var/lib/apt/lists/*
ENV GFLOW_CLI_TELEMETRY=0 DO_NOT_TRACK=1 GFLOW_CLI_UPDATE_CHECK=false \
    GFLOW_CLI_INCIDENT_CAPTURE=false GFLOW_CLI_HISTORY_PROMPTS=redacted GFLOW_CLI_BROWSER_ENGINE=playwright \
    GFLOW_CLI_HOME=/data/google-flow/gflow GFLOW_CLI_OUTPUT_DIR=/data/google-flow/output \
    GOOGLE_FLOW_STATE_DIR=/data/google-flow/state GOOGLE_FLOW_DASHBOARD_DIR=/app/dashboard \
    GOOGLE_FLOW_WEB_ROOT=/app/webclient \
    GOOGLE_FLOW_DESKTOP_CONTROL=/app/runtime/desktop-control.py \
    GOOGLE_FLOW_RTC_FILE=/run/user/1000/google-flow-rtc.json \
    PATH="/opt/google-flow/bin:${PATH}"
COPY pyproject.toml ./
COPY google_flow_gateway/ google_flow_gateway/
COPY dashboard/ dashboard-source/
RUN python3 -m venv /opt/google-flow && python -m pip install --no-cache-dir . && python -m pip check && \
    python -c "from importlib.metadata import version; assert version('gflow-cli') == '0.101.0'" && \
    python -m google_flow_gateway.dashboard --source dashboard-source --output dashboard && rm -rf dashboard-source
COPY runtime/ runtime/
RUN cp -a /opt/bdwind/webrtc /app/webclient && \
    cp runtime/webclient/embed.js /app/webclient/studio-embed.js && \
    python runtime/webclient/prepare.py /app/webclient && \
    chmod 755 runtime/entrypoint.sh runtime/desktop-control.py && test "$(id -u beagle)" = 1000
USER 1000:1000
EXPOSE 8000 8001 48083
ENTRYPOINT ["/app/runtime/entrypoint.sh"]
