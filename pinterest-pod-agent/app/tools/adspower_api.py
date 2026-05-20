from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings


class AdsPowerError(RuntimeError):
    pass


@dataclass(frozen=True)
class AdsPowerBrowserInfo:
    profile_id: str
    status: str | None = None
    ws_puppeteer: str | None = None
    ws_selenium: str | None = None
    debug_port: str | None = None
    webdriver: str | None = None
    raw: dict[str, Any] | None = None


class AdsPowerClient:
    """Client for AdsPower's local API.

    This wrapper only starts, stops, and queries user-managed profiles through
    AdsPower's documented local API. It does not configure fingerprints,
    proxies, stealth scripts, or platform risk-control bypasses.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.adspower_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.adspower_api_key
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else settings.adspower_timeout_seconds
        self.cache_dir = Path("var/adspower")

    def list_profiles(self, *, page: int = 1, page_size: int = 5) -> list[dict[str, Any]]:
        """List AdsPower profiles (public health-check helper)."""
        payload = self._get(
            "/api/v1/user/list",
            params={"page": page, "page_size": page_size},
        )
        data = payload.get("data") or {}
        return data.get("list") or []

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        return self._get("/api/v1/user/list", params={"user_id": profile_id})

    def start_profile(
        self,
        profile_id: str,
        *,
        open_tabs: int = 0,
        wait_seconds: float = 20.0,
    ) -> AdsPowerBrowserInfo:
        try:
            payload = self._get(
                "/api/v1/browser/start",
                params={"user_id": profile_id, "open_tabs": open_tabs},
            )
            info = self._browser_info(profile_id, payload)
            if info.ws_puppeteer:
                self._cache_endpoint(profile_id, info.ws_puppeteer)
                return info
        except AdsPowerError as exc:
            last_error: AdsPowerError | None = exc
        else:
            last_error = None

        info = self.wait_for_profile_endpoint(profile_id, timeout_seconds=wait_seconds)
        if info.ws_puppeteer:
            self._cache_endpoint(profile_id, info.ws_puppeteer)
            return info
        if last_error:
            raise last_error
        raise AdsPowerError(f"AdsPower profile did not expose a browser endpoint: {profile_id}")

    def stop_profile(self, profile_id: str) -> dict[str, Any]:
        return self._get("/api/v1/browser/stop", params={"user_id": profile_id})

    def get_profile_status(self, profile_id: str) -> AdsPowerBrowserInfo:
        payload = self._get("/api/v1/browser/active", params={"user_id": profile_id})
        info = self._browser_info(profile_id, payload)
        if info.ws_puppeteer:
            self._cache_endpoint(profile_id, info.ws_puppeteer)
        return info

    def get_playwright_endpoint(self, profile_id: str) -> str:
        try:
            info = self.get_profile_status(profile_id)
        except (AdsPowerError, httpx.HTTPError):
            try:
                info = self.start_profile(profile_id)
            except AdsPowerError:
                cached = self._read_cached_endpoint(profile_id)
                if cached:
                    return cached
                raise
        if not info.ws_puppeteer:
            try:
                info = self.start_profile(profile_id)
            except AdsPowerError:
                cached = self._read_cached_endpoint(profile_id)
                if cached:
                    return cached
                raise
        if not info.ws_puppeteer:
            raise AdsPowerError(f"AdsPower profile has no puppeteer endpoint: {profile_id}")
        return info.ws_puppeteer

    def wait_for_profile_endpoint(
        self,
        profile_id: str,
        *,
        timeout_seconds: float = 20.0,
        interval_seconds: float = 1.0,
    ) -> AdsPowerBrowserInfo:
        deadline = time.monotonic() + timeout_seconds
        last_info = AdsPowerBrowserInfo(profile_id=profile_id, status="Unknown")
        while time.monotonic() < deadline:
            try:
                last_info = self.get_profile_status(profile_id)
                if last_info.ws_puppeteer:
                    self._cache_endpoint(profile_id, last_info.ws_puppeteer)
                    return last_info
            except AdsPowerError:
                pass
            time.sleep(interval_seconds)
        return last_info

    def _cache_endpoint(self, profile_id: str, endpoint: str) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        (self.cache_dir / f"{profile_id}.ws").write_text(endpoint, encoding="utf-8")

    def _read_cached_endpoint(self, profile_id: str) -> str | None:
        path = self.cache_dir / f"{profile_id}.ws"
        if not path.exists():
            return None
        endpoint = path.read_text(encoding="utf-8").strip()
        return endpoint or None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        transport = httpx.HTTPTransport(retries=0)
        with httpx.Client(timeout=self.timeout_seconds, transport=transport) as client:
            response = client.get(url, params=params, headers=self._headers())
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AdsPowerError(f"AdsPower HTTP {response.status_code}: {response.text}") from exc
        payload = response.json()
        if payload.get("code") != 0:
            raise AdsPowerError(payload.get("msg") or f"AdsPower API failed: {path}")
        return payload

    @staticmethod
    def _browser_info(profile_id: str, payload: dict[str, Any]) -> AdsPowerBrowserInfo:
        data = payload.get("data") or {}
        ws = data.get("ws") or {}
        return AdsPowerBrowserInfo(
            profile_id=profile_id,
            status=data.get("status"),
            ws_puppeteer=ws.get("puppeteer"),
            ws_selenium=ws.get("selenium"),
            debug_port=str(data["debug_port"]) if data.get("debug_port") is not None else None,
            webdriver=data.get("webdriver"),
            raw=payload,
        )
