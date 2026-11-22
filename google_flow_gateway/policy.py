from urllib.parse import urlsplit

DEFAULTS = {
    "concurrency": 1,
    "min_interval_seconds": 0,
    "daily_submit_limit": 0,
    "per_job_credit_limit": 0,
    "daily_credit_limit": 0,
    "on_unusual_activity": "pause",
    "cooldown_minutes": 0,
    "on_submission_unknown": "hold",
}
ACTIONS = {"on_unusual_activity": ("pause", "continue"), "on_submission_unknown": ("hold", "pause")}
REASONS = {"manual", "unusual_activity", "authorization_required", "credit_limit", "submission_unknown", "gpu_lease_lost"}
DEFAULT_ALLOWED_PROXY_HOSTS = ("openclash.openclash.svc.cluster.local",)


class Error(Exception):
    def __init__(self, code, status=422, field=None):
        super().__init__(code)
        self.code, self.status, self.field = code, status, field


def validate_proxy_url(proxy_url, allowed_hosts=DEFAULT_ALLOWED_PROXY_HOSTS):
    if proxy_url is None or proxy_url == "":
        return ""
    if not isinstance(proxy_url, str) or len(proxy_url) > 256:
        raise Error("invalid_field", field="proxy_url")
    cleaned = proxy_url.strip()
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
        or parsed.port is None
        or not (1024 <= parsed.port <= 65535)
    ):
        raise Error("invalid_field", field="proxy_url")
    allowed = {h.strip().lower() for h in allowed_hosts if h and h.strip()}
    if parsed.hostname.lower() not in allowed:
        raise Error("invalid_field", field="proxy_url")
    return f"{parsed.scheme}://{parsed.hostname.lower()}:{parsed.port}"


def validate(changes):
    if not isinstance(changes, dict):
        raise Error("invalid_policy")
    for field, value in changes.items():
        if field not in DEFAULTS:
            raise Error("unknown_field", field=field)
        if field in ACTIONS:
            valid = value in ACTIONS[field]
        else:
            valid = type(value) is int and int(field == "concurrency") <= value <= 2**31 - 1
        if not valid:
            raise Error("invalid_field", field=field)
