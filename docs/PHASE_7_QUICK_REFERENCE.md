# Phase 7 Multi-Source Intelligence — Quick Reference

## When Each Source Is Called

| Stage | Source | Why |
|---|---|---|
| Scanner discovery | GMGN trending + GeckoTerminal new pools + Nansen screener | Diversified candidate feed |
| Pre-scoring enrichment | DexScreener + Birdeye | Price/liquidity/volume confirmation |
| Smart money signal | Nansen smart_money_trend + GMGN cluster | Highest-confidence alpha signal |
| Pre-trade safety | Token Verifier (5 sources voting) | Multi-source veto on any critical flag |
| Pre-trade timing | Pump.fun graduation tracker | 70-95% sweet spot detection |
| (Optional) AI advisor | Phase 6 LLM | Last line of defense |

## Smart Money Trend Matrix (Nansen)

| 24h | 7d | Verdict | Bot Score Bonus |
|---|---|---|---|
| + | + | sustained_accumulation | +20 |
| + | - | fresh_entry | +15 (with smart_trader_flow) / +5 (without) |
| - | + | reducing | -10 |
| - | - | distribution | -25 |
| mixed | mixed | mixed | 0 |

## Cluster Signal Strength (GMGN)

| Signal | Score Bonus | Threshold |
|---|---|---|
| WEAK | 0 | 1 wallet |
| MEDIUM | +5 | 2-3 wallets |
| STRONG | +15 | 3+ wallets within 30 min |
| VERY_STRONG | +20 | 3+ wallets within 15 min + KOL participation |

## 5-Source Token Verifier Weights

| Source | Weight | Reason |
|---|---|---|
| Rugcheck | 1.0 | Most direct safety scoring |
| GMGN | 0.9 | Strong tag detection |
| Nansen | 0.8 | Indicators-based |
| Birdeye | 0.7 | General overview |
| DexScreener | 0.5 | Liquidity signal only |

Verdict thresholds:
- weighted_safety_score >= 0.7 → SAFE
- 0.4-0.7 → WARN
- < 0.4 → REJECT
- Any source flags critical (honeypot/LP unlocked/mint not renounced) → REJECT regardless

## Pump.fun Graduation Bonus

| Progress % | Bonus |
|---|---|
| 70-95% (sweet spot) | +10 |
| 50-70% | +5 |
| 30-50% | +2 |
| <30% | 0 |
| 100% (graduated) | -5 (already moved to Raydium) |
| Non-Pump.fun | 0 |

## Cost Caps

- `NANSEN_DAILY_CREDIT_CAP=300` — halts Nansen calls if exceeded
- Birdeye: 50 RPM free, paid for more
- Rugcheck: rate-limited public, no hard cap
- DexScreener: rate-limited public
- Pump.fun: rate-limited public

## Troubleshooting

| Error | Solution |
|---|---|
| `NANSEN_API_KEY not set` | Sign up at app.nansen.ai, add to .env |
| Nansen `CREDITS_EXHAUSTED` | Cap exceeded, wait reset or upgrade plan |
| Pump.fun 530 | Need browser User-Agent (client handles this) |
| Birdeye `Unauthorized` | Some endpoints need BIRDEYE_API_KEY |
| GMGN-CLI not found | `sudo npm install -g gmgn-cli` |
| Nansen-CLI not found | `sudo npm install -g nansen-cli` |
