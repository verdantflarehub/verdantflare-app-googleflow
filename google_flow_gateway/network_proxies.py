"""Consume authorized VerdantFlare Network (app-network) system proxies."""
import ipaddress
import json
import re
import uuid
from urllib.parse import quote, urlsplit

import httpx

from .policy import Error


def parse_proxy_ref(value):
    if value is None or value == {"mode": "default"} or value == {"mode": "direct"}:
        return {"mode": "default"}
    if not isinstance(value, dict) or value.get("mode") != "proxy":
        raise Error("invalid_field", field="proxy_ref")
    pid = value.get("proxy_id")
    rev = value.get("revision")
    try:
        parsed = uuid.UUID(str(pid))
        if not parsed.int or str(parsed) != pid:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise Error("invalid_field", field="proxy_ref") from None
    if type(rev) is not int or not 1 <= rev <= 2**53 - 1:
        raise Error("invalid_field", field="proxy_ref")
    return {"mode": "proxy", "proxy_id": pid, "revision": rev}


class NetworkProxies:
    def __init__(self, origin, token, *, client=None):
        parsed = urlsplit(origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or len(token) < 32 or any(c.isspace() for c in token)):
            raise ValueError("invalid_network_configuration")
        self.origin = origin.rstrip("/")
        self.token = token
        self.client = client or httpx.Client(trust_env=False, follow_redirects=False, timeout=5)

    def close(self):
        self.client.close()

    def _request(self, organization, action, payload):
        try:
            with self.client.stream(
                "POST",
                f"{self.origin}/internal/channel-proxies/v1/{action}",
                headers={"Authorization": f"Bearer {self.token}", "X-Organization-Id": organization},
                json={"schema_version": 1, **payload},
            ) as response:
                if response.status_code != 200:
                    raise Error("proxy_unavailable", 502)
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 256 * 1024:
                        raise Error("proxy_unavailable", 502)
                result = json.loads(data)
                if not isinstance(result, dict) or result.get("schema_version") != 1:
                    raise Error("proxy_unavailable", 502)
                return result
        except Error:
            raise
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise Error("proxy_unavailable", 502) from None

    def list(self, organization):
        result = self._request(organization, "list", {})
        items = result.get("items")
        if not isinstance(items, list):
            raise Error("proxy_unavailable", 502)
        return {"schema_version": 1, "items": items}

    def resolve_url(self, organization, reference):
        ref = parse_proxy_ref(reference)
        if ref["mode"] == "default":
            return "", ""
        items = self.list(organization)["items"]
        matched = next((i for i in items if i.get("proxy_id") == ref["proxy_id"]), None)
        name = matched.get("name", "") if matched else ""
        value = self._request(organization, "resolve", {"proxy_id": ref["proxy_id"], "revision": ref["revision"]})
        protocol = value.get("protocol", "")
        host = value.get("host", "")
        port = value.get("port")
        if protocol not in {"http", "https", "socks5", "socks5h"} or not isinstance(host, str) or not host:
            raise Error("proxy_unavailable", 502)
        if ":" in host:
            host = f"[{ipaddress.IPv6Address(host)}]"
        elif not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise Error("proxy_unavailable", 502)
        if type(port) is not int or not 1 <= port <= 65535:
            raise Error("proxy_unavailable", 502)
        user = value.get("username") or ""
        pwd = value.get("password") or ""
        # OpenClash mixed ports (1080-1099) speak both HTTP and SOCKS5; using http:// avoids Chrome/gflow socks5h quirks.
        scheme = "http" if (protocol in {"socks5", "socks5h"} and host.startswith("openclash.") and not user) else ("socks5" if protocol == "socks5h" else protocol)
        auth = f"{quote(user, safe='')}:{quote(pwd, safe='')}@" if user else ""
        return f"{scheme}://{auth}{host}:{port}", name
