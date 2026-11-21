#!/bin/bash
set -euo pipefail
umask 077

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/1000}"
export DISPLAY="${DISPLAY:-:99}"
mkdir -p "$XDG_RUNTIME_DIR" "$GFLOW_CLI_HOME" "$GFLOW_CLI_OUTPUT_DIR" "$GOOGLE_FLOW_STATE_DIR"
for dir in "$XDG_RUNTIME_DIR" "$GFLOW_CLI_HOME" "$GFLOW_CLI_OUTPUT_DIR" "$GOOGLE_FLOW_STATE_DIR"; do
    chmod 700 "$dir" 2>/dev/null || true
done

conf=/app/runtime/supervisord.conf
stopping=false
signaled=false
trap 'stopping=true' TERM INT

/usr/bin/supervisord -c "$conf" &
supervisor=$!
while kill -0 "$supervisor" 2>/dev/null; do
    status=$(/usr/bin/supervisorctl -c "$conf" status gateway 2>/dev/null) || true
    if [[ $status =~ (EXITED|FATAL|BACKOFF|STOPPED) ]]; then
        rc=1
        if [[ $status == *EXITED* || $status == *STOPPED* ]]; then
            rc=$(/usr/bin/python3 -c 'from supervisor.xmlrpc import SupervisorTransport; from xmlrpc.client import ServerProxy; print(ServerProxy("http://127.0.0.1", transport=SupervisorTransport(None, None, "unix:///run/user/1000/google-flow-supervisor.sock")).supervisor.getProcessInfo("gateway")["exitstatus"])') || rc=1
        fi
        kill -TERM "$supervisor"
        wait "$supervisor" || true
        exit "$rc"
    fi
    if [[ $stopping == true && $signaled == false ]]; then
        /usr/bin/supervisorctl -c "$conf" signal TERM gateway >/dev/null 2>&1 || true
        signaled=true
    fi
    sleep 0.5
done
wait "$supervisor" || exit $?
exit 1
