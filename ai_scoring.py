import logging
import os

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


class AIScorer:
    """AI-powered deal scoring using OpenAI.

    Score scale is 0-100 (matches the Creators Deal Finder flow spec: the AI
    evaluates discount quality, product popularity, brand, reselling potential,
    and overall attractiveness, then a deal needs >= minimum_score (default 50)
    to pass and get posted to Discord).
    """

    def __init__(self, config):
        self.config = config
        self.enabled = config.get("ai", {}).get("enabled", False)
        self.minimum_score = config.get("ai", {}).get("minimum_score", 50)
        self.model = config.get("ai", {}).get("openai_model", "gpt-4-mini")

        if self.enabled and OpenAI:
            api_key = config.get("ai", {}).get("openai_api_key") or os.getenv("OPENAI_API_KEY")
            if api_key and not api_key.startswith("sk-YOUR"):
                self.client = OpenAI(api_key=api_key)
            else:
                logger.warning("[AI] OpenAI API key not configured")
                self.client = None
        else:
            self.client = None

    def score_deal(self, title, asin):
        """Score a deal 0-100 based on title and ASIN.

        The model is asked to weigh discount quality, product popularity,
        brand, reselling potential, and overall attractiveness into one
        0-100 score (0=terrible, 100=amazing).
        """
        if not self.enabled or not self.client:
            return 50.0  # Default (borderline-pass) score if AI disabled

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Rate this Amazon product deal from 0-100 (0=terrible, 100=amazing). "
                            "Weigh discount quality, product popularity, brand, reselling potential, "
                            f"and overall attractiveness.\n\nTitle: {title}\n\nASIN: {asin}\n\n"
                            "Respond with ONLY a number 0-100."
                        )
                    }
                ],
                temperature=0.3,
                max_tokens=10
            )

            text = response.choices[0].message.content.strip()
            score = float(''.join(c for c in text if c.isdigit() or c == '.'))
            score = max(0, min(100, score))  # Clamp 0-100

            logger.info(f"[AI] Scored {asin}: {score}/100")
            return score

        except Exception as e:
            logger.error(f"[AI] Scoring error: {e}")
            return 50.0
