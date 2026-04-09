from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib import error, request


class ApiError(RuntimeError):
    pass


DEFAULT_BASE_URLS = (
    "http://127.0.0.1:3000",
    "http://127.0.0.1:80",
    "http://127.0.0.1:8080",
    "http://localhost:3000",
    "http://localhost:80",
    "http://localhost:8080",
)


def _probe_url(base_url: str) -> bool:
    candidate = str(base_url or "").strip().rstrip("/")
    if not candidate:
        return False
    try:
        req = request.Request(f"{candidate}/api/v2/onprem/status", method="GET", headers={"Accept": "application/json"})
        with request.urlopen(req, timeout=2) as response:
            return response.status < 500
    except error.HTTPError as exc:
        return exc.code < 500
    except OSError:
        return False


def detect_api_base_url(preferred: str | None = None) -> str:
    candidates: list[str] = []
    if preferred:
        candidates.append(preferred)
    env_candidate = str(os.getenv("PACKETSAFARI_API_BASE_URL") or "").strip()
    if env_candidate:
        candidates.append(env_candidate)
    candidates.extend(DEFAULT_BASE_URLS)

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate or "").strip().rstrip("/")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if _probe_url(normalized):
            return normalized
    return str(preferred or env_candidate or DEFAULT_BASE_URLS[0]).strip().rstrip("/")


@dataclass(slots=True)
class LocalApiClient:
    base_url: str = detect_api_base_url()

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def with_detected_base_url(self) -> "LocalApiClient":
        self.base_url = detect_api_base_url(self.base_url)
        return self

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(self._url(path), method=method.upper(), data=data, headers=headers)
        try:
            with request.urlopen(req, timeout=15) as response:
                raw = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ApiError(f"{exc.code} {exc.reason}: {body}") from exc
        except OSError as exc:
            raise ApiError(str(exc)) from exc
        try:
            payload_obj = json.loads(raw or "{}")
        except json.JSONDecodeError as exc:
            raise ApiError(f"Invalid JSON from {path}: {exc}") from exc
        if not isinstance(payload_obj, dict):
            raise ApiError(f"Unexpected response from {path}")
        return payload_obj

    def onboarding_schema(self) -> dict:
        return self._request("GET", "/api/v2/onprem/onboarding/schema")

    def onboarding_validate(self, values: dict) -> dict:
        return self._request("POST", "/api/v2/onprem/onboarding/validate", {"values": values})

    def onboarding_save_draft(self, values: dict) -> dict:
        return self._request("POST", "/api/v2/onprem/onboarding/draft", {"values": values})

    def onboarding_finalize(self, values: dict) -> dict:
        return self._request("POST", "/api/v2/onprem/onboarding/finalize", {"values": values})

    def onprem_status(self) -> dict:
        return self._request("GET", "/api/v2/onprem/status")

    def health_summary(self) -> dict[str, object]:
        try:
            payload = self.onprem_status().get("data") or {}
            if not isinstance(payload, dict):
                payload = {}
            return {
                "reachable": True,
                "baseUrl": self.base_url,
                "onPremises": bool(payload.get("onPremises")),
                "onboardingMode": bool(payload.get("onboardingMode")),
                "raw": payload,
            }
        except ApiError as exc:
            return {
                "reachable": False,
                "baseUrl": self.base_url,
                "error": str(exc),
            }
