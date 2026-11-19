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


class Error(Exception):
    def __init__(self, code, status=422, field=None):
        super().__init__(code)
        self.code, self.status, self.field = code, status, field


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
