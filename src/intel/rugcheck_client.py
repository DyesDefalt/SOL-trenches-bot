"""
Rugcheck.xyz async client (public, no key required).

Rugcheck analyzes Solana token contracts for common rug-pull vectors:
unlocked LP, unrenounced mint authority, honeypot patterns, high
concentration, etc. Returns a risk score and labelled risk list.

Base URL: https://api.rugcheck.xyz/v1
No authentication needed.
Rate limit: not published — we use conservative 2 req/sec.

Score interpretation:
  score_normalised 0.0 = safest possible, higher = riskier
  We default to rejecting anything above 1.0.

Docs: https://api.rugcheck.xyz/swagger-ui
"""

from __future__ import annotations

from src.clients.base import BaseHTTPClient, HTTPError
from src.infra.cache import cached
from src.infra.logger import get_logger
from src.infra.rate_limiter import TokenBucket

log = get_logger(__name__)

# Risk names that cause unconditional rejection, regardless of score
CRITICAL_RISK_NAMES: frozenset[str] = frozenset(
    {
        "LP unlocked",
        "Mint authority not renounced",
        "Single holder > 50%",
        "Honeypot",
    }
)


class RugcheckClient:
    """
    Async Rugcheck client.

    Usage::

        async with RugcheckClient() as client:
            report = await client.get_token_report(mint)
            safe, reasons = RugcheckClient.is_safe(report)
    """

    BASE_URL = "https://api.rugcheck.xyz/v1"

    def __init__(self) -> None:
        self._http = BaseHTTPClient(
            base_url=self.BASE_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "solana-sniper-bot/0.1",
            },
            timeout=20.0,
            max_retries=3,
        )
        # Very conservative — public unauthenticated endpoint
        self._limiter = TokenBucket(rps=2.0, burst=5.0, name="rugcheck")

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "RugcheckClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        await self._limiter.acquire()
        return await self._http.get(path, params=params)

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    @cached(prefix="rugcheck:report", ttl=300)
    async def get_token_report(self, mint: str) -> dict:
        """
        Full Rugcheck report for a token.

        Returns full JSON including:
          - score: numeric raw score (lower = safer)
          - score_normalised: float (0.0 = safe)
          - risks: list of {name, description, level, score}
          - markets, holders, tokenMeta, etc.

        Cached 300s — on-chain state changes slowly.
        """
        try:
            return await self._get(f"/tokens/{mint}/report")
        except HTTPError as e:
            log.error("rugcheck_report_error", mint=mint, status=e.status, error=str(e))
            return {}

    @cached(prefix="rugcheck:summary", ttl=300)
    async def get_token_report_summary(self, mint: str) -> dict:
        """
        Lightweight summary version of the Rugcheck report.

        Faster and cheaper than full report. Suitable for bulk scanning.
        Returns subset of fields: score, score_normalised, risks.
        """
        try:
            return await self._get(f"/tokens/{mint}/report/summary")
        except HTTPError as e:
            log.error("rugcheck_summary_error", mint=mint, status=e.status, error=str(e))
            return {}

    # ------------------------------------------------------------------
    # Safety helpers
    # ------------------------------------------------------------------

    @staticmethod
    def is_safe(
        report: dict,
        max_score_normalised: float = 1.0,
    ) -> tuple[bool, list[str]]:
        """
        Evaluate whether a Rugcheck report meets safety criteria.

        Args:
            report: dict returned by get_token_report or get_token_report_summary
            max_score_normalised: reject if score_normalised > this threshold

        Returns:
            (safe: bool, risk_names: list[str])
              safe=True means passes all checks
              risk_names is always populated (empty = no risks detected)
        """
        if not report:
            # No data — treat as unsafe (unknown)
            return False, ["report_unavailable"]

        score_norm: float = float(report.get("score_normalised", 0.0))
        risks: list[dict] = report.get("risks", [])
        risk_names: list[str] = [r.get("name", "") for r in risks]

        failed_reasons: list[str] = []

        # Score threshold
        if score_norm > max_score_normalised:
            failed_reasons.append(f"score_normalised={score_norm:.2f}>{max_score_normalised}")

        # Critical risk veto — any match is an instant fail
        for name in risk_names:
            if name in CRITICAL_RISK_NAMES:
                failed_reasons.append(name)

        if failed_reasons:
            log.debug("rugcheck_unsafe", reasons=failed_reasons)
            return False, risk_names

        return True, risk_names
