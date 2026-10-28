"""A small client for the design engine (Grain): the product was designed there, and it judges what the phone scanned
against that design (PLAN.md section 2, decision 2026-10-28 "Grain judges; the phone captures and asks").

Only its HTTP surface is used (base URL and key from GRAIN_URL / GRAIN_API_KEY, `Authorization: Bearer <key>`):

    POST /projects/{project}/scans?name=<file>.zip&units=m   the zipped session folder; answers the scan record
    POST /call                                               the judgement of one scan, with readings and a symptom
    GET  /projects/{project}/electrical/plan                 which meter checks to ask for (404: not available yet)
    GET  /projects/{project}/bom/{ref}                       one BOM row (404: no such row)
    POST /projects/{project}/chat                            ask it to change the design (wait + queue)

Every answer body is saved under the session folder as `grain/<kind>-<scan_id>-<timestamp>.json`, so a real run can
be read back and replayed. Nothing here invents an answer: a missing URL means `enabled` is False and nothing is called.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx2

SCAN_TIMEOUT_S = 120.0
CALL_TIMEOUT_S = 600.0
CHAT_TIMEOUT_S = 900.0
PLAN_TIMEOUT_S = 60.0
BOM_TIMEOUT_S = 30.0
GRAIN_DIR = "grain"


class GrainError(RuntimeError):
    """The design engine answered with an error, or could not be reached. `status` is the HTTP status (None when
    the request never got an answer); `detail` is its own explanation, never the key."""

    def __init__(self, what: str, status: int | None, detail: str):
        self.what, self.status, self.detail = what, status, detail
        super().__init__(f"{what}: {detail}" + (f" (HTTP {status})" if status else ""))


def _detail(resp: httpx2.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300] or resp.reason_phrase
    d = body.get("detail", body) if isinstance(body, dict) else body
    return (d if isinstance(d, str) else json.dumps(d))[:300]


def stamp(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%S%fZ")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_") or "x"


class GrainClient:
    """`transport` replaces the network (tests pass one that records the request); everything else is the same code."""

    def __init__(self, url: str | None, key: str | None = None, project: str | None = None, *,
                 transport: httpx2.BaseTransport | None = None):
        self.url = (url or "").strip().rstrip("/")
        self.key = key or None
        self.project = (project or "").strip() or None
        self._transport = transport

    @classmethod
    def from_settings(cls, settings: Any = None, *, transport: httpx2.BaseTransport | None = None) -> GrainClient:
        if settings is None:
            from repairos.config import get_settings
            settings = get_settings()
        key = settings.grain_api_key.get_secret_value() if settings.grain_api_key else None
        return cls(settings.grain_url, key, settings.grain_project, transport=transport)

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    # ---------------------------------------------------------------- plumbing
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"} if self.key else {}

    def _request(self, method: str, path: str, *, timeout: float, what: str, params: Mapping[str, Any] | None = None,
                 json_body: Any = None, content: bytes | None = None, content_type: str | None = None) -> Any:
        if not self.enabled:
            raise GrainError(what, None, "no design engine configured (GRAIN_URL is not set)")
        headers = self._headers()
        if content_type:
            headers["Content-Type"] = content_type
        try:
            with httpx2.Client(timeout=timeout, transport=self._transport) as c:
                resp = c.request(method, self.url + path, params=params, json=json_body, content=content,
                                 headers=headers)
        except httpx2.HTTPError as e:
            raise GrainError(what, None, f"{type(e).__name__}: {e}"[:300]) from e
        if resp.status_code >= 300:
            raise GrainError(what, resp.status_code, _detail(resp))
        try:
            return resp.json()
        except ValueError as e:
            raise GrainError(what, resp.status_code, "the answer was not JSON") from e

    @staticmethod
    def record(directory: Path | None, kind: str, scan_id: str, body: Any, now: datetime | None = None) -> Path | None:
        """Save one answer as it came: `<directory>/<kind>-<scan_id>-<timestamp>.json`."""
        if directory is None:
            return None
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_slug(kind)}-{_slug(scan_id)}-{stamp(now)}.json"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path

    @staticmethod
    def _p(project: str) -> str:
        return f"/projects/{quote(project, safe='')}"

    # --------------------------------------------------------------- endpoints
    def post_scan(self, project: str, name: str, data: bytes, *, units: str = "m", scan_id: str,
                  record_to: Path | None = None) -> dict:
        """The zipped session folder; returns the scan record (`id`, `lea.transform_arkit_m_to_design_mm`, ...)."""
        out = self._request("POST", f"{self._p(project)}/scans", timeout=SCAN_TIMEOUT_S, what="upload the scan",
                            params={"name": name, "units": units}, content=data, content_type="application/zip")
        self.record(record_to, "scan", scan_id, out)
        return out

    def call(self, project: str, grain_scan_id: str, *, question: str | None, readings: list[dict] | None = None,
             judge: bool = True, scan_id: str, record_to: Path | None = None) -> dict:
        """The judgement of one uploaded scan. `readings` replaces the readings stored with the scan record."""
        out = self._request("POST", "/call", timeout=CALL_TIMEOUT_S, what="judge the scan",
                            json_body=call_body(project, grain_scan_id, question, readings, judge))
        self.record(record_to, "call", scan_id, out)
        return out

    def plan(self, project: str, suspects: list[str] | None = None, *, scan_id: str,
             record_to: Path | None = None) -> dict:
        """The meter checks to ask for. `{}` when the design engine has no plan endpoint yet (404)."""
        try:
            out = self._request("GET", f"{self._p(project)}/electrical/plan", timeout=PLAN_TIMEOUT_S,
                                what="get the meter plan",
                                params={"suspects": ",".join(suspects)} if suspects else None)
        except GrainError as e:
            if e.status == 404:
                return {}
            raise
        self.record(record_to, "plan", scan_id, out)
        return out

    def bom_row(self, project: str, ref: str, *, scan_id: str, record_to: Path | None = None) -> dict | None:
        """One BOM row, or None when the design has no row of that name (404)."""
        try:
            out = self._request("GET", f"{self._p(project)}/bom/{quote(ref, safe='')}", timeout=BOM_TIMEOUT_S,
                                what="get the BOM row")
        except GrainError as e:
            if e.status == 404:
                return None
            raise
        self.record(record_to, f"bom-{ref}", scan_id, out)
        return out if isinstance(out, dict) else None

    def part_file_url(self, project: str, name: str, fmt: str = "stl") -> str:
        """Where one made part's file is (the design engine's own URL; opening it needs the key)."""
        return f"{self.url}{self._p(project)}/bom/part/{quote(name, safe='')}/file?format={fmt}"

    def chat(self, project: str, message: str, *, scan_id: str, record_to: Path | None = None) -> dict:
        """One message to the design engine's agent; returns when its turn ends. Waits for a running turn."""
        out = self._request("POST", f"{self._p(project)}/chat", timeout=CHAT_TIMEOUT_S, what="ask for a design change",
                            json_body={"message": message, "wait": True, "queue": True})
        self.record(record_to, "chat", scan_id, out)
        return out


def call_body(project: str, grain_scan_id: str, question: str | None, readings: list[dict] | None,
              judge: bool = True) -> dict:
    """The body of POST /call (a pure function, so the shape is testable)."""
    body: dict[str, Any] = {"project": project, "scan": {"id": grain_scan_id}, "judge": judge}
    if question:
        body["question"] = question
    if readings:
        body["readings"] = readings
    return body
