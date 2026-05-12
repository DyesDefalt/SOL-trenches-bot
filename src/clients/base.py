"""
Shared async HTTP client base.

Features:
- IPv4 only (GMGN tidak support IPv6, lebih aman default semua client begitu)
- Retry dengan exponential backoff via tenacity
- Timeout reasonable
- Structured logging untuk request/response
- orjson untuk performa parsing
"""

from __future__ import annotations

from typing import Any

import httpx
import orjson
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.infra.logger import get_logger

log = get_logger(__name__)


class HTTPError(Exception):
    """Base error untuk HTTP client failures."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class RateLimitError(HTTPError):
    """429 Too Many Requests. Special-cased karena retry-able dengan backoff lebih panjang."""

    def __init__(self, message: str, retry_after: float | None = None, body: str | None = None) -> None:
        super().__init__(message, status=429, body=body)
        self.retry_after = retry_after


class BaseHTTPClient:
    """
    Async HTTP client base dengan retry, timeout, structured logging.

    IPv4 enforcement: dilakukan di OS level (sysctl) di VPS, BUKAN di Python level.
    Lihat dokumen Phase 0 step 6.F untuk disable IPv6 di VPS.

    httpx default akan honor HTTPS_PROXY env var (perlu untuk sandbox/proxied env).
    """

    def __init__(
        self,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        force_ipv4: bool = True,  # legacy param, kept for API compat — OS-level enforcement
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self._headers = headers or {}

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=timeout,
            follow_redirects=True,
            http2=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "BaseHTTPClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        retry_on_429: bool = True,
    ) -> dict[str, Any]:
        """
        Make HTTP request dengan retry logic. Returns parsed JSON dict.

        Raises:
            RateLimitError: 429 + sudah max retries
            HTTPError: non-2xx response setelah retries
        """
        url = path if path.startswith("http") else f"{self.base_url}{path}"

        # Tenacity-based retry untuk transient errors
        retry_exceptions = (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.ReadError,
        )
        if retry_on_429:
            retry_exceptions = (*retry_exceptions, RateLimitError)

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type(retry_exceptions),
            reraise=True,
        ):
            with attempt:
                return await self._do_request(method, url, params, json, headers)

        raise RuntimeError("unreachable")

    async def _do_request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None,
        json: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method=method,
                url=url,
                params=params,
                json=json,
                headers=headers,
            )
        except httpx.HTTPError as e:
            log.warning("http_error", method=method, url=url, error=str(e))
            raise

        if response.status_code == 429:
            retry_after = self._parse_retry_after(response)
            log.warning(
                "rate_limited",
                method=method,
                url=url,
                retry_after=retry_after,
                body=response.text[:200],
            )
            raise RateLimitError(
                f"Rate limited at {url}",
                retry_after=retry_after,
                body=response.text,
            )

        if response.status_code >= 400:
            log.error(
                "http_status_error",
                method=method,
                url=url,
                status=response.status_code,
                body=response.text[:500],
            )
            raise HTTPError(
                f"{response.status_code} at {url}: {response.text[:200]}",
                status=response.status_code,
                body=response.text,
            )

        # Parse pakai orjson kalau bisa (lebih cepat)
        try:
            return orjson.loads(response.content)
        except orjson.JSONDecodeError:
            log.warning("json_decode_error", url=url, body=response.text[:200])
            return {"raw": response.text}

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        """Parse Retry-After header. Bisa berupa detik (int) atau HTTP-date."""
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            return None
        try:
            return float(retry_after)
        except ValueError:
            # HTTP date format — skip parsing untuk simpel
            return None

    async def get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return await self.request("POST", path, **kwargs)
