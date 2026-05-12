"""
Multi-Source Token Verifier — Phase 7d.

Aggregasi keputusan keamanan token dari 5 sumber via majority voting berbobot.
Sumber: rugcheck, gmgn, nansen, birdeye, dexscreener.

Setiap sumber menghasilkan SourceVote (safe/unsafe/None).
Verdict final:
  - REJECT  : ada critical flag ATAU weighted_safety_score < 0.4
  - WARN    : weighted_safety_score < 0.7
  - SAFE    : weighted_safety_score >= 0.7

Cache TTL 300s — cukup untuk scanner loop tapi tidak terlalu stale.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from src.infra.cache import cached
from src.infra.logger import get_logger

if TYPE_CHECKING:
    from src.clients.gmgn import GMGNClient
    from src.intel.birdeye_client import BirdeyeClient
    from src.intel.dexscreener_client import DexscreenerClient
    from src.intel.nansen_client import NansenClient
    from src.intel.rugcheck_client import RugcheckClient

log = get_logger(__name__)

# Critical flags yang langsung trigger REJECT — urutan penting untuk messaging
CRITICAL_FLAGS = frozenset({"honeypot", "mint_not_renounced", "lp_unlocked"})

# Bobot reliabilitas per sumber (0-1)
SOURCE_WEIGHTS: dict[str, float] = {
    "rugcheck": 1.0,  # paling langsung untuk safety scoring
    "gmgn": 0.9,      # security_score + tag detection yang bagus
    "nansen": 0.8,    # lebih ke risk indicators
    "birdeye": 0.7,   # general overview
    "dexscreener": 0.5,  # liquidity/volume signal saja, bukan security
}

# Minimum liquidity untuk dexscreener vote as "safe"
DEXSCREENER_MIN_LIQUIDITY_USD = 8_000.0
DEXSCREENER_MIN_VOLUME_24H_USD = 5_000.0


@dataclass
class SourceVote:
    """Vote dari satu sumber data tentang keamanan token."""

    source: str  # "gmgn", "nansen", "rugcheck", "dexscreener", "birdeye"
    is_safe: bool | None  # None = sumber tidak tersedia / tidak ada pendapat
    risk_flags: list[str] = field(default_factory=list)
    confidence: float = 1.0  # 0-1, bobot reliabilitas sumber
    raw_data: dict = field(default_factory=dict)


@dataclass
class TokenVerification:
    """Hasil agregasi verifikasi dari semua sumber."""

    token_address: str
    chain: str
    votes: list[SourceVote]

    # Aggregated decision
    safe_votes: int = 0
    unsafe_votes: int = 0
    unavailable_count: int = 0
    weighted_safety_score: float = 0.0  # 0-1
    verdict: Literal["SAFE", "WARN", "REJECT"] = "WARN"
    critical_flags: list[str] = field(default_factory=list)  # honeypot, mint_not_renounced, lp_unlocked, etc


# --------------------------------------------------------------------------
# Source-specific vote parsers
# --------------------------------------------------------------------------

def _parse_rugcheck_vote(report: dict) -> SourceVote:
    """
    Rugcheck: gunakan helper is_safe() dari rugcheck_client.
    Fallback ke score_normalised <= 1 kalau helper tidak ada di report.
    """
    from src.intel.rugcheck_client import is_safe as rugcheck_is_safe  # type: ignore[import]

    risk_flags: list[str] = []
    try:
        safe, issues = rugcheck_is_safe(report, max_score=500)
        risk_flags.extend(issues)
        # Cek critical flags dari issues
        for issue in issues:
            lower = issue.lower()
            if "honeypot" in lower:
                risk_flags.append("honeypot")
            if "mint" in lower and "renounced" in lower:
                risk_flags.append("mint_not_renounced")
            if "lp" in lower and ("unlocked" in lower or "not burned" in lower):
                risk_flags.append("lp_unlocked")
        return SourceVote(
            source="rugcheck",
            is_safe=safe,
            risk_flags=risk_flags,
            confidence=SOURCE_WEIGHTS["rugcheck"],
            raw_data=report,
        )
    except Exception as exc:
        log.warning("rugcheck_vote_parse_error", error=str(exc))
        return SourceVote(
            source="rugcheck",
            is_safe=None,
            confidence=SOURCE_WEIGHTS["rugcheck"],
        )


def _parse_gmgn_vote(token_info: dict) -> SourceVote:
    """
    GMGN: safe jika:
    - is_honeypot != 1
    - rug_ratio < 0.3
    - renounced_mint == 1 (Solana — mint authority revoked)
    """
    risk_flags: list[str] = []

    is_honeypot = token_info.get("is_honeypot", 0)
    rug_ratio = float(token_info.get("rug_ratio", 0.0))
    renounced_mint = token_info.get("renounced_mint", 0)

    if is_honeypot == 1:
        risk_flags.append("honeypot")
    if rug_ratio >= 0.3:
        risk_flags.append(f"high_rug_ratio ({rug_ratio:.2f})")
    if renounced_mint != 1:
        risk_flags.append("mint_not_renounced")

    # Juga cek tag-based signals dari GMGN
    tags: list[str] = token_info.get("tags", []) or []
    for tag in tags:
        if isinstance(tag, str):
            lower = tag.lower()
            if "honeypot" in lower:
                risk_flags.append("honeypot")
            elif "rug" in lower:
                risk_flags.append("rug_risk_tag")

    # LP status
    lp_burned = token_info.get("renounced_lp") or token_info.get("lp_burned", 0)
    if lp_burned == 0:
        risk_flags.append("lp_unlocked")

    is_safe = is_honeypot != 1 and rug_ratio < 0.3 and renounced_mint == 1
    return SourceVote(
        source="gmgn",
        is_safe=is_safe,
        risk_flags=risk_flags,
        confidence=SOURCE_WEIGHTS["gmgn"],
        raw_data=token_info,
    )


def _parse_nansen_vote(indicators: dict) -> SourceVote:
    """
    Nansen: safe jika tidak ada high-risk indicators.
    Checks risk_score field dan specific risk flags.
    """
    risk_flags: list[str] = []

    risk_score = float(indicators.get("risk_score", 0.0))
    high_risk = indicators.get("high_risk_indicators", []) or []

    for indicator in high_risk:
        risk_flags.append(str(indicator))

    # Normalize risk_score 0-10 scale assumed; >7 = high risk
    if risk_score > 7.0:
        risk_flags.append(f"nansen_high_risk_score ({risk_score:.1f})")
        is_safe = False
    elif risk_score > 4.0:
        is_safe = len(high_risk) == 0
    else:
        is_safe = True

    return SourceVote(
        source="nansen",
        is_safe=is_safe,
        risk_flags=risk_flags,
        confidence=SOURCE_WEIGHTS["nansen"],
        raw_data=indicators,
    )


def _parse_birdeye_vote(security_data: dict) -> SourceVote:
    """
    Birdeye: token_security flags — check honeypot, freeze authority, mint authority.
    """
    risk_flags: list[str] = []

    # Birdeye token security fields
    is_honeypot = security_data.get("is_honeypot") or security_data.get("honeypot", False)
    freeze_authority = security_data.get("freeze_authority") or security_data.get("freezeAuthority")
    mint_authority = security_data.get("mint_authority") or security_data.get("mintAuthority")
    top10_holder_pct = float(security_data.get("top10HolderPercent", 0.0) or 0.0)

    if is_honeypot:
        risk_flags.append("honeypot")
    if freeze_authority:
        risk_flags.append("freeze_authority_active")
    if mint_authority:
        risk_flags.append("mint_not_renounced")
    if top10_holder_pct > 80.0:
        risk_flags.append(f"high_concentration ({top10_holder_pct:.0f}% top10)")

    is_safe = not is_honeypot and not mint_authority
    return SourceVote(
        source="birdeye",
        is_safe=is_safe,
        risk_flags=risk_flags,
        confidence=SOURCE_WEIGHTS["birdeye"],
        raw_data=security_data,
    )


def _parse_dexscreener_vote(pair_data: dict) -> SourceVote:
    """
    Dexscreener: bukan security per se — liquidity + volume signal.
    Safe jika liquidity >= 8k AND volume_24h >= 5k.
    """
    risk_flags: list[str] = []

    liquidity_usd = float(pair_data.get("liquidity", {}).get("usd", 0.0) or 0.0)
    volume_24h = float(pair_data.get("volume", {}).get("h24", 0.0) or 0.0)

    if liquidity_usd < DEXSCREENER_MIN_LIQUIDITY_USD:
        risk_flags.append(f"low_liquidity (${liquidity_usd:.0f})")
    if volume_24h < DEXSCREENER_MIN_VOLUME_24H_USD:
        risk_flags.append(f"low_volume_24h (${volume_24h:.0f})")

    is_safe = (
        liquidity_usd >= DEXSCREENER_MIN_LIQUIDITY_USD
        and volume_24h >= DEXSCREENER_MIN_VOLUME_24H_USD
    )
    return SourceVote(
        source="dexscreener",
        is_safe=is_safe,
        risk_flags=risk_flags,
        confidence=SOURCE_WEIGHTS["dexscreener"],
        raw_data=pair_data,
    )


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def _aggregate_votes(token_address: str, chain: str, votes: list[SourceVote]) -> TokenVerification:
    """
    Hitung weighted_safety_score dan tentukan verdict dari list SourceVote.
    """
    critical_flags: list[str] = []
    safe_votes = 0
    unsafe_votes = 0
    unavailable_count = 0
    weight_sum = 0.0
    weighted_safe_sum = 0.0

    for vote in votes:
        # Kumpulkan semua risk flags
        for flag in vote.risk_flags:
            if flag in CRITICAL_FLAGS and flag not in critical_flags:
                critical_flags.append(flag)

        if vote.is_safe is None:
            unavailable_count += 1
            continue

        if vote.is_safe:
            safe_votes += 1
            weighted_safe_sum += vote.confidence
        else:
            unsafe_votes += 1

        weight_sum += vote.confidence

    # weighted_safety_score: sum of weights of safe votes / sum of weights of available votes
    weighted_safety_score = weighted_safe_sum / weight_sum if weight_sum > 0 else 0.0

    # Verdict logic
    if critical_flags:
        # Critical flag → langsung REJECT
        verdict: Literal["SAFE", "WARN", "REJECT"] = "REJECT"
    elif weighted_safety_score < 0.4:
        verdict = "REJECT"
    elif weighted_safety_score < 0.7:
        verdict = "WARN"
    else:
        verdict = "SAFE"

    return TokenVerification(
        token_address=token_address,
        chain=chain,
        votes=votes,
        safe_votes=safe_votes,
        unsafe_votes=unsafe_votes,
        unavailable_count=unavailable_count,
        weighted_safety_score=round(weighted_safety_score, 4),
        verdict=verdict,
        critical_flags=critical_flags,
    )


# --------------------------------------------------------------------------
# Main class
# --------------------------------------------------------------------------

class TokenVerifier:
    """
    Multi-source token verifier — parallel fetch dari 5 sumber, aggregasi voting berbobot.

    Usage:
        verifier = TokenVerifier(gmgn, nansen, rugcheck, dexscreener, birdeye)
        result = await verifier.verify("TokenAddress123")
        if result.verdict == "REJECT":
            return  # skip token
    """

    def __init__(
        self,
        gmgn_client: GMGNClient,
        nansen_client: NansenClient,
        rugcheck_client: RugcheckClient,
        dexscreener_client: DexscreenerClient,
        birdeye_client: BirdeyeClient,
    ) -> None:
        self._gmgn = gmgn_client
        self._nansen = nansen_client
        self._rugcheck = rugcheck_client
        self._dexscreener = dexscreener_client
        self._birdeye = birdeye_client

    @cached(prefix="verifier:", ttl=300)
    async def verify(self, token_address: str, chain: str = "sol") -> TokenVerification:
        """
        Full 5-source verification. Semua source di-fetch parallel.
        Source yang gagal → SourceVote dengan is_safe=None (tidak di-skip, hanya tidak di-count).
        """
        log.debug("token_verify_start", token=token_address, chain=chain)

        # Parallel fetch semua sumber
        results = await asyncio.gather(
            self._fetch_rugcheck(token_address),
            self._fetch_gmgn(token_address, chain),
            self._fetch_nansen(token_address, chain),
            self._fetch_birdeye(token_address),
            self._fetch_dexscreener(token_address),
            return_exceptions=True,
        )

        votes: list[SourceVote] = []
        source_names = ["rugcheck", "gmgn", "nansen", "birdeye", "dexscreener"]

        for name, result in zip(source_names, results):
            if isinstance(result, BaseException):
                log.warning(
                    "token_verify_source_error",
                    token=token_address,
                    source=name,
                    error=str(result),
                )
                # Sumber gagal → vote unavailable
                votes.append(SourceVote(
                    source=name,
                    is_safe=None,
                    confidence=SOURCE_WEIGHTS.get(name, 0.5),
                ))
            else:
                votes.append(result)

        verification = _aggregate_votes(token_address, chain, votes)

        log.info(
            "token_verify_done",
            token=token_address,
            verdict=verification.verdict,
            score=verification.weighted_safety_score,
            critical=verification.critical_flags,
            safe_votes=verification.safe_votes,
            unsafe_votes=verification.unsafe_votes,
            unavailable=verification.unavailable_count,
        )
        return verification

    async def quick_safety_check(self, token_address: str, chain: str = "sol") -> bool:
        """
        Fast 2-source check (rugcheck + gmgn) untuk hot path.
        Returns True kalau token looks safe, False kalau suspicious.
        """
        results = await asyncio.gather(
            self._fetch_rugcheck(token_address),
            self._fetch_gmgn(token_address, chain),
            return_exceptions=True,
        )

        votes: list[SourceVote] = []
        for name, result in zip(["rugcheck", "gmgn"], results):
            if isinstance(result, BaseException):
                log.warning("quick_check_source_error", token=token_address, source=name, error=str(result))
                votes.append(SourceVote(source=name, is_safe=None, confidence=SOURCE_WEIGHTS[name]))
            else:
                votes.append(result)

        verification = _aggregate_votes(token_address, chain, votes)
        return verification.verdict == "SAFE"

    # ------------------------------------------------------------------
    # Internal fetch helpers — tiap sumber di-isolate supaya error tidak propagate
    # ------------------------------------------------------------------

    async def _fetch_rugcheck(self, token_address: str) -> SourceVote:
        report = await self._rugcheck.get_token_report(token_address)
        if not report:
            return SourceVote(source="rugcheck", is_safe=None, confidence=SOURCE_WEIGHTS["rugcheck"])
        return _parse_rugcheck_vote(report)

    async def _fetch_gmgn(self, token_address: str, chain: str) -> SourceVote:
        token_info = await self._gmgn.get_token_info(token_address, chain=chain)  # type: ignore[arg-type]
        if not token_info:
            return SourceVote(source="gmgn", is_safe=None, confidence=SOURCE_WEIGHTS["gmgn"])
        return _parse_gmgn_vote(token_info)

    async def _fetch_nansen(self, token_address: str, chain: str) -> SourceVote:
        indicators = await self._nansen.get_indicators(chain, token_address)
        if not indicators:
            return SourceVote(source="nansen", is_safe=None, confidence=SOURCE_WEIGHTS["nansen"])
        return _parse_nansen_vote(indicators)

    async def _fetch_birdeye(self, token_address: str) -> SourceVote:
        security_data = await self._birdeye.get_token_security(token_address)
        if not security_data:
            return SourceVote(source="birdeye", is_safe=None, confidence=SOURCE_WEIGHTS["birdeye"])
        return _parse_birdeye_vote(security_data)

    async def _fetch_dexscreener(self, token_address: str) -> SourceVote:
        pair_data = await self._dexscreener.get_top_pair_for_token(token_address)
        if not pair_data:
            return SourceVote(source="dexscreener", is_safe=None, confidence=SOURCE_WEIGHTS["dexscreener"])
        return _parse_dexscreener_vote(pair_data)
