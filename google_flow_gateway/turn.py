import base64
import hashlib
import hmac
import secrets
import time

from .policy import Error


def credentials(urls, secret, clock=time.time):
    if not secret or not urls or any(not url.startswith(("turn:", "turns:")) for url in urls):
        raise Error("desktop_unavailable", 503)
    expires = int(clock()) + 900
    username = f"{expires}:{secrets.token_hex(12)}"
    signature = hmac.digest(secret.encode(), username.encode(), hashlib.sha1)
    return {"iceTransportPolicy": "relay", "expires_at": expires, "iceServers": [
        {"urls": urls, "username": username, "credential": base64.b64encode(signature).decode()}]}
