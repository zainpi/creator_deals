import logging
import os

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


class AIScorer:
    """AI-powered deal scoring using OpenAI."""
    
    def __init__(self, config):
        self.config = config
        self.enabled = config.get("ai", {}).get("enabled", False)
        self.minimum_score = config.get("ai", {}).get("minimum_score", 7)
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
        """Score a deal 1-10 based on title and ASIN."""
        if not self.enabled or not self.client:
            return 5.0  # Default score if AI disabled
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "user",
                        "content": f"Rate this Amazon product deal 1-10 (1=terrible, 10=amazing): {title}\n\nASIN: {asin}\n\nRespond with ONLY a number 1-10."
                    }
                ],
                temperature=0.3,
                max_tokens=10
            )

            text = response.choices[0].message.content.strip()
            score = float(''.join(c for c in text if c.isdigit() or c == '.'))
            score = max(1, min(10, score))  # Clamp 1-10

            logger.info(f"[AI] Scored {asin}: {score}/10")
            return score
            
        except Exception as e:
            logger.error(f"[AI] Scoring error: {e}")
            return 5.0
