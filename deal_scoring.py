"""
deal_scoring.py — deterministic deal scoring.

The AI is responsible ONLY for identifying the product and estimating price
ranges (retail_low/high, resale_low/high). EVERY number below — discount %,
estimated profit, resell score, buying score, penalties and the overall
gating score — is computed here in plain Python, so results are reproducible
and always consistent with the AI's estimated ranges. The model never does math.

Business rules (confirmed with the operator):
  * Estimated profit = resale midpoint - buy price   (no marketplace fees)
  * The resale/retail *midpoint* drives profit and every score
  * Penalties fire on: weak discount vs retail, wide/uncertain range,
    thin/negative margin
  * Buying score = how far below estimated retail you're buying (value)
  * Resell score = estimated profit & margin (quality of the flip)

All thresholds/weights live in config.yml under `scoring:` and can be tuned
without touching this file; DEFAULTS below apply when a key is absent.
"""

from __future__ import annotations

DEFAULTS = {
    # Which point of a low/high range to use ("mid", "low", or "high").
    "resale_point": "mid",

    # Buying score — discount depth vs estimated retail.
    "target_discount_pct": 50.0,      # buying this far below retail = full 100

    # Resell score — profit & margin.
    "target_profit": 50.0,            # absolute profit (currency) earning full marks
    "target_roi_pct": 50.0,           # ROI% earning full marks
    "resell_profit_weight": 0.5,      # profit vs ROI blend inside the resell score
    "resell_roi_weight": 0.5,

    # Overall gating score = blend of the two scores, minus penalties.
    "overall_buying_weight": 0.5,
    "overall_resell_weight": 0.5,
    "neutral_score": 50.0,            # used when no usable AI estimate exists

    # Penalties (maximum point deductions).
    "weak_discount_threshold": 15.0,  # discount% below this starts penalising
    "weak_discount_penalty": 20.0,
    "wide_range_threshold": 0.60,     # (high-low)/mid above this starts penalising
    "wide_range_penalty": 15.0,
    "wide_range_cap": 1.50,           # spread ratio at which the full penalty applies
    "thin_margin_min_profit": 10.0,   # currency — below this is "thin"
    "thin_margin_min_roi_pct": 15.0,  # % — below this is "thin"
    "thin_margin_penalty": 25.0,      # thin (but still positive) margin
    "negative_margin_penalty": 45.0,  # profit <= 0
}


def _cfg(config):
    """Merge config['scoring'] over DEFAULTS (ignoring null overrides)."""
    merged = dict(DEFAULTS)
    if isinstance(config, dict):
        section = config.get("scoring") or {}
        if isinstance(section, dict):
            for k, v in section.items():
                if v is not None:
                    merged[k] = v
    return merged


def _num(x):
    try:
        if x is None:
            return None
        return float(x)
    except (TypeError, ValueError):
        return None


def _round(x, ndigits=2):
    return round(x, ndigits) if isinstance(x, (int, float)) else x


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _mid(low, high, point="mid"):
    """Collapse a low/high range to a single figure per `point`."""
    lo, hi = _num(low), _num(high)
    vals = [v for v in (lo, hi) if v is not None]
    if not vals:
        return None
    if lo is None or hi is None:
        return vals[0]
    if point == "low":
        return min(lo, hi)
    if point == "high":
        return max(lo, hi)
    return (lo + hi) / 2.0


def compute_scores(estimate, buy_price, config=None, context=None):
    """Turn an AI price estimate + buy price into every downstream number.

    `estimate` is a dict with any of: product, retail_low, retail_high,
    resale_low, resale_high. Missing/garbage values degrade gracefully to a
    neutral, non-crashing result. Returns a flat dict of computed fields
    (see keys built below); `ai_score` mirrors `overall_score` so existing
    callers that gate on `ai_score >= minimum_score` keep working unchanged.
    """
    cfg = _cfg(config)
    point = cfg["resale_point"]
    buy = _num(buy_price)
    est = estimate or {}

    retail_low, retail_high = _num(est.get("retail_low")), _num(est.get("retail_high"))
    resale_low, resale_high = _num(est.get("resale_low")), _num(est.get("resale_high"))
    retail_mid = _mid(retail_low, retail_high, point)
    resale_mid = _mid(resale_low, resale_high, point)

    result = {
        "product": est.get("product") or est.get("name") or None,
        "retail_low": retail_low,
        "retail_high": retail_high,
        "retail_mid": _round(retail_mid),
        "resale_low": resale_low,
        "resale_high": resale_high,
        "resale_mid": _round(resale_mid),
        "buy_price": _round(buy),
        "estimated_profit": None,
        "roi_pct": None,
        "margin_pct": None,
        "discount_pct": None,
        "range_uncertainty": None,
        "buying_score": None,
        "resell_score": None,
        "overall_score": None,
        "penalties": [],
        "ai_score": None,     # mirror of overall_score for backward-compat callers
        "ai_reason": "",
        "estimate_ok": False,
    }

    # No usable estimate / buy price -> neutral, non-crashing result.
    if buy is None or buy <= 0 or resale_mid is None:
        neutral = float(cfg["neutral_score"])
        result["overall_score"] = neutral
        result["ai_score"] = neutral
        result["ai_reason"] = "No usable AI price estimate — neutral score"
        return result

    result["estimate_ok"] = True

    # ---- core money math (deterministic) ----
    profit = resale_mid - buy
    roi_pct = (profit / buy * 100.0) if buy > 0 else None
    margin_pct = (profit / resale_mid * 100.0) if resale_mid > 0 else None
    discount_pct = ((retail_mid - buy) / retail_mid * 100.0) if (retail_mid and retail_mid > 0) else None

    spread = None
    if resale_low is not None and resale_high is not None and resale_mid > 0:
        spread = abs(resale_high - resale_low) / resale_mid

    # ---- buying score: discount depth vs estimated retail ----
    if discount_pct is None:
        buying = float(cfg["neutral_score"])
    else:
        buying = _clamp(discount_pct / cfg["target_discount_pct"] * 100.0)

    # ---- resell score: profit & margin (ROI) ----
    profit_component = _clamp(profit / cfg["target_profit"] * 100.0)
    roi_component = _clamp((roi_pct or 0.0) / cfg["target_roi_pct"] * 100.0)
    resell = _clamp(
        cfg["resell_profit_weight"] * profit_component
        + cfg["resell_roi_weight"] * roi_component
    )

    base_overall = _clamp(
        cfg["overall_buying_weight"] * buying
        + cfg["overall_resell_weight"] * resell
    )

    # ---- penalties ----
    penalties = []
    total_penalty = 0.0

    # weak discount vs retail
    wd_thr = cfg["weak_discount_threshold"]
    if discount_pct is not None and wd_thr > 0 and discount_pct < wd_thr:
        frac = (wd_thr - discount_pct) / wd_thr
        pts = round(_clamp(cfg["weak_discount_penalty"] * frac, 0.0, cfg["weak_discount_penalty"]), 1)
        if pts > 0:
            penalties.append({"type": "weak_discount", "points": pts,
                              "detail": f"discount {discount_pct:.0f}% < {wd_thr:.0f}%"})
            total_penalty += pts

    # wide / uncertain resale range
    wr_thr = cfg["wide_range_threshold"]
    if spread is not None and spread > wr_thr:
        span = max(cfg["wide_range_cap"] - wr_thr, 1e-6)
        frac = _clamp((spread - wr_thr) / span, 0.0, 1.0)
        pts = round(cfg["wide_range_penalty"] * frac, 1)
        if pts > 0:
            penalties.append({"type": "wide_range", "points": pts,
                              "detail": f"resale spread {spread * 100:.0f}% of mid"})
            total_penalty += pts

    # thin / negative margin
    if profit <= 0:
        pts = round(cfg["negative_margin_penalty"], 1)
        if pts > 0:
            penalties.append({"type": "negative_margin", "points": pts,
                              "detail": f"profit {profit:.2f}"})
            total_penalty += pts
    elif profit < cfg["thin_margin_min_profit"] or (roi_pct is not None and roi_pct < cfg["thin_margin_min_roi_pct"]):
        pts = round(cfg["thin_margin_penalty"], 1)
        if pts > 0:
            penalties.append({"type": "thin_margin", "points": pts,
                              "detail": f"profit {profit:.2f}, ROI {(roi_pct or 0):.0f}%"})
            total_penalty += pts

    overall = _clamp(base_overall - total_penalty)

    # ---- fill result ----
    result["estimated_profit"] = _round(profit)
    result["roi_pct"] = _round(roi_pct)
    result["margin_pct"] = _round(margin_pct)
    result["discount_pct"] = _round(discount_pct)
    result["range_uncertainty"] = _round(spread, 3)
    result["buying_score"] = _round(buying, 1)
    result["resell_score"] = _round(resell, 1)
    result["overall_score"] = _round(overall, 1)
    result["penalties"] = penalties
    result["ai_score"] = result["overall_score"]
    result["ai_reason"] = _reason(result)
    return result


#: Fields that get merged into a `product` dict (DB columns + display + gating).
#: `ai_score`/`ai_reason` mirror the computed overall score so every existing
#: caller that gates on `ai_score >= minimum_score` keeps working unchanged.
PRODUCT_FIELDS = (
    "retail_low", "retail_high", "resale_low", "resale_high", "resale_mid",
    "estimated_profit", "discount_pct", "resell_score", "buying_score",
    "overall_score",
)


def to_product_fields(scoring):
    """Flatten a compute_scores() result into the keys a product dict carries."""
    fields = {k: scoring.get(k) for k in PRODUCT_FIELDS}
    fields["ai_score"] = scoring.get("overall_score")
    fields["ai_reason"] = scoring.get("ai_reason", "")
    return fields


def _reason(r):
    """Human-readable one-liner explaining how the overall score was reached."""
    parts = []
    buy, retail_mid = r.get("buy_price"), r.get("retail_mid")
    if buy is not None:
        d = r.get("discount_pct")
        d_str = f" (-{d:.0f}% vs retail)" if isinstance(d, (int, float)) else ""
        parts.append(f"Buy {buy:.2f}{d_str}")
    if r.get("resale_mid") is not None:
        parts.append(f"resale {r['resale_mid']:.2f}")
    if r.get("estimated_profit") is not None:
        roi = r.get("roi_pct")
        roi_str = f", ROI {roi:.0f}%" if isinstance(roi, (int, float)) else ""
        parts.append(f"profit {r['estimated_profit']:.2f}{roi_str}")
    parts.append(
        f"Buying {r.get('buying_score')}, Resell {r.get('resell_score')}, "
        f"Overall {r.get('overall_score')}"
    )
    if r.get("penalties"):
        pen = "; ".join(f"{p['type']} -{p['points']}" for p in r["penalties"])
        parts.append(f"Penalties: {pen}")
    return " | ".join(parts)
