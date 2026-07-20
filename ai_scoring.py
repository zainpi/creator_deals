import json
import logging
import os

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


# Local (non-euro) currency hint per marketplace, purely to steer the model's
# price magnitudes. All downstream math is currency-agnostic.
_CURRENCY = {
    "DE": "EUR", "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR", "BE": "EUR",
    "GB": "GBP", "UK": "GBP", "US": "USD",
}


class AIScorer:
    """AI price/identity estimator.

    The model's ONLY job is to identify the product and estimate realistic
    price ranges (retail + resale). It is explicitly told NOT to compute
    discounts, profit, or any score — all of that math is done deterministically
    in deal_scoring.py so results stay consistent with the estimated ranges and
    the model can't hallucinate numbers that don't add up.
    """

    def __init__(self, config):
        self.config = config
        self.enabled = config.get("ai", {}).get("enabled", False)
        self.minimum_score = config.get("ai", {}).get("minimum_score", 50)
        self.model = config.get("ai", {}).get("openai_model", "gpt-4o-mini")

        if self.enabled and OpenAI:
            api_key = config.get("ai", {}).get("openai_api_key") or os.getenv("OPENAI_API_KEY")
            if api_key and not api_key.startswith("sk-YOUR"):
                self.client = OpenAI(api_key=api_key)
            else:
                logger.warning("[AI] OpenAI API key not configured")
                self.client = None
        else:
            self.client = None

    # ------------------------------------------------------------------ estimate
    def estimate(self, title, asin, marketplace=None, price=None, category=None):
        """Identify the product and estimate price ranges.

        Returns a dict:
            {product, retail_low, retail_high, resale_low, resale_high}
        or {} when the AI is disabled / unavailable / errors (callers treat an
        empty estimate as "score neutral", never as a crash).
        """
        if not self.enabled or not self.client:
            return {}

        currency = _CURRENCY.get(str(marketplace or "DE").upper(), "EUR")
        lines = [
            "You are a product-identification and resale-pricing analyst for a "
            "deal-finding tool. Identify the product from the Amazon listing and "
            "estimate realistic price RANGES. Do NOT compute discounts, profit, "
            "margins, or any score — estimate prices ONLY.",
            "",
            f"Marketplace: {marketplace or 'DE'} (values in {currency}).",
            f"Title: {title}",
            f"ASIN: {asin}",
        ]
        if category:
            lines.append(f"Category: {category}")
        if price is not None:
            lines.append(f"Current listing price: {price} {currency}")
        lines += [
            "",
            "Return ONLY minified JSON with exactly these keys, all plain numbers "
            "(no currency symbols, no ranges-as-strings):",
            '{"product":"<short product name>","retail_low":<num>,'
            '"retail_high":<num>,"resale_low":<num>,"resale_high":<num>}',
            "",
            "retail_low/high  = typical NEW retail selling-price range.",
            "resale_low/high  = realistic price this item would RESELL for on the "
            "secondary/used/open-box market.",
            "Use conservative, defensible figures; keep low <= high.",
        ]
        prompt = "\n".join(lines)

        try:
            text = self._chat(prompt)
            est = _parse_estimate(text)
            if est:
                logger.info(
                    f"[AI] Estimate {asin}: retail {est.get('retail_low')}-{est.get('retail_high')} "
                    f"/ resale {est.get('resale_low')}-{est.get('resale_high')}"
                )
            else:
                logger.warning(f"[AI] Unparseable estimate for {asin}: {str(text)[:200]!r}")
            return est
        except Exception as e:
            logger.error(f"[AI] Estimate error for {asin}: {e}")
            return {}

    def _chat(self, prompt):
        """Call the model, preferring JSON mode but degrading gracefully if the
        configured model doesn't support response_format."""
        kwargs = dict(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=200,
        )
        try:
            resp = self.client.chat.completions.create(
                response_format={"type": "json_object"}, **kwargs
            )
        except Exception:
            resp = self.client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    # -------------------------------------------------------------- compat shim
    def score_deal(self, title, asin):
        """Deprecated: previous single-number AI score. Kept so any un-migrated
        caller degrades to a neutral score instead of crashing. New code should
        call estimate() and feed it to deal_scoring.compute_scores()."""
        logger.debug("[AI] score_deal() is deprecated; use estimate() + deal_scoring")
        return float(self.minimum_score)


def _parse_estimate(text):
    """Extract the estimate JSON from a model response and coerce the numbers.
    Returns {} if nothing usable is found."""
    if not text:
        return {}
    raw = text.strip()
    if not raw.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return {}
        raw = raw[start:end + 1]
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}

    out = {}
    product = data.get("product") or data.get("name")
    if product:
        out["product"] = str(product)[:200]
    for key in ("retail_low", "retail_high", "resale_low", "resale_high"):
        val = _coerce_number(data.get(key))
        if val is not None:
            out[key] = val
    # Only useful if we got at least one resale bound to price against.
    if "resale_low" not in out and "resale_high" not in out:
        return {}
    return out


def _coerce_number(x):
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        cleaned = "".join(c for c in x if c.isdigit() or c in ".,").replace(",", "")
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None
    return None
