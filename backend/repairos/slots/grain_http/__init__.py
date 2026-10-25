"""GRAIN=http: Grain's REST API over urllib (stdlib). GRAIN_URL (and GRAIN_KEY, sent as a bearer key, when the Grain
server requires one) come from backend/.env through config.require: the backend holds them, the phone never does
(A6). Every call has a bounded timeout and a size cap on what it accepts back; a failure is a GrainError with the
status, never a silent default. Turns are detached (start, then poll), so no long-lived stream is held open.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from repairos.config import get_settings
from repairos.registry import register
from repairos.slots.grain import GrainError

TIMEOUT_S = 60.0           # an upload of a 50 MB scan on a slow link; reads are far quicker
MAX_RESPONSE_BYTES = 200 * 1024 * 1024
MAX_SCAN_BYTES = 50 * 1024 * 1024       # Grain's own limits, checked before sending
MAX_PHOTO_BYTES = 20 * 1024 * 1024


@register("GRAIN", "http")
class GrainHTTP:
    def __init__(self, base_url: str | None = None, key: str | None = None, timeout_s: float = TIMEOUT_S):
        s = get_settings()
        self.base = (base_url or s.require("GRAIN_URL", "GRAIN=http")).rstrip("/")
        self.key = key if key is not None else (s.grain_key.get_secret_value() if s.grain_key else "")
        self.timeout_s = timeout_s

    def _call(self, method: str, path: str, *, body: bytes | None = None, content_type: str | None = None,
              query: dict | None = None, raw: bool = False) -> Any:
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {"Accept": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                data = r.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as e:
            detail = e.read(4000).decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            raise GrainError(e.code, str(detail)) from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise GrainError(None, str(getattr(e, "reason", e))) from e
        if len(data) > MAX_RESPONSE_BYTES:
            raise GrainError(None, f"{path}: response over {MAX_RESPONSE_BYTES} bytes")
        if raw:
            return data
        return json.loads(data) if data else None

    def _json(self, method: str, path: str, payload: Any) -> Any:
        return self._call(method, path, body=json.dumps(payload).encode(), content_type="application/json")

    @staticmethod
    def _pid(project_id: str) -> str:
        return urllib.parse.quote(project_id, safe="")

    def projects(self) -> list[dict[str, Any]]:
        return self._call("GET", "/projects") or []

    def create_project(self, name: str) -> dict[str, Any]:
        return self._json("POST", "/projects", {"name": name[:120]})

    def project(self, project_id: str) -> dict[str, Any]:
        return self._call("GET", f"/projects/{self._pid(project_id)}")

    def upload_scan(self, project_id: str, name: str, data: bytes, units: str) -> dict[str, Any]:
        if len(data) > MAX_SCAN_BYTES:
            raise GrainError(413, f"scan is {len(data)} bytes; Grain takes at most {MAX_SCAN_BYTES}")
        return self._call("POST", f"/projects/{self._pid(project_id)}/scans", body=data,
                          content_type="application/octet-stream", query={"name": name, "units": units})

    def upload_photo(self, project_id: str, scan_id: str | None, name: str, data: bytes) -> dict[str, Any]:
        if len(data) > MAX_PHOTO_BYTES:
            raise GrainError(413, f"photo is {len(data)} bytes; Grain takes at most {MAX_PHOTO_BYTES}")
        path = (f"/projects/{self._pid(project_id)}/scans/{urllib.parse.quote(scan_id, safe='')}/photos"
                if scan_id else f"/projects/{self._pid(project_id)}/photos")
        return self._call("POST", path, body=data, content_type="image/jpeg", query={"name": name})

    def scans(self, project_id: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/projects/{self._pid(project_id)}/scans") or []

    def write_file(self, project_id: str, path: str, content: str) -> None:
        if not path.startswith("src/"):
            raise ValueError("Lea writes only under src/")
        self._json("PUT", f"/projects/{self._pid(project_id)}/files/{urllib.parse.quote(path)}", {"content": content})

    def read_file(self, project_id: str, path: str) -> bytes:
        return self._call("GET", f"/projects/{self._pid(project_id)}/files/{urllib.parse.quote(path)}", raw=True)

    def files(self, project_id: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/projects/{self._pid(project_id)}/files") or []

    def start_turn(self, project_id: str, message: str) -> dict[str, Any]:
        return self._json("POST", f"/projects/{self._pid(project_id)}/chat", {"message": message, "detach": True})

    def messages(self, project_id: str) -> list[dict[str, Any]]:
        return self._call("GET", f"/projects/{self._pid(project_id)}/messages") or []

    def report(self, project_id: str) -> dict[str, Any] | None:
        return self._call("GET", f"/projects/{self._pid(project_id)}/report")
