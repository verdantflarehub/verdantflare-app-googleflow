#!/bin/bash
set -euo pipefail
. /etc/beagle-wind-vnc/runtime-env.sh
umask 077

gpu=$(nvidia-smi --query-gpu=uuid --format=csv,noheader) || exit 78
if [[ ! $gpu =~ ^GPU-[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]]; then
    echo 'Expected exactly one GPU UUID' >&2
    exit 78
fi
nodes=()
for node in /dev/dri/renderD*; do
    if [[ -c $node && ! -L $node ]]; then nodes+=("$node"); fi
done
if [[ ${#nodes[@]} != 1 ]]; then
    echo 'Expected exactly one injected render device' >&2
    exit 78
fi
export BDWIND_SMITHAY_RENDER_NODE="${nodes[0]}"
/etc/beagle-wind-vnc/entrypoint.sh true
mkdir -p "$XDG_RUNTIME_DIR" "$GFLOW_CLI_HOME" "$GFLOW_CLI_OUTPUT_DIR" "$GOOGLE_FLOW_STATE_DIR"
chmod 700 "$XDG_RUNTIME_DIR" "$GFLOW_CLI_HOME" "$GFLOW_CLI_OUTPUT_DIR" "$GOOGLE_FLOW_STATE_DIR"

# Keep the control socket available while the gateway settles and releases its lease.
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
