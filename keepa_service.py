import keepa
import logging

logger = logging.getLogger(__name__)


class KeepaService:
    """
    Keepa validation service for deal discovery.
    
    Can reuse Keepa parsing logic from dealsbrowser.py
    and productTrackerV2.py for production.
    """
    
    def __init__(self, config_or_key):
        # Support both config dict and plain API key
        if isinstance(config_or_key, dict):
            api_key = config_or_key.get("keepa", {}).get("api_key")
        else:
            api_key = config_or_key
        
        self.api = keepa.Keepa(api_key)
        self.cache = {}

    def validate_deal(self, asin, current_price, domain="DE"):
        """
        Validate deal using Keepa stats.
        
        Returns dict with:
        - avg90: 90-day average price
        - drop_percent: current drop % from 90-day avg
        - sales_rank: current sales rank
        - monthly_sold: estimated monthly sold count
        """
        
        try:
            # Check cache first
            if asin in self.cache:
                return self.cache[asin]
            
            products = self.api.query(
                asin,
                domain=domain,
                stats=90,
                history=False
            )

            if not products:
                logger.warning(f"[KEEPA] No product data for {asin}")
                return None

            product = products[0]
            stats = product.get("stats", {})

            # Extract 90-day average
            avg90 = stats.get("avg90_BUY_BOX_SHIPPING")

            if avg90 is None:
                logger.warning(f"[KEEPA] No 90-day stats for {asin}")
                return None

            # Convert from Keepa cents to euros
            avg90 = avg90 / 100

            # Calculate drop percentage
            drop_percent = round(
                ((avg90 - current_price) / avg90) * 100,
                2
            ) if avg90 > 0 else 0

            result = {
                "avg90": avg90,
                "drop_percent": drop_percent,
                "sales_rank": product.get("salesRankReference"),
                "monthly_sold": stats.get("salesRankDrops90"),
                "asin": asin,
                "validated_at": str(__import__('datetime').datetime.now())
            }
            
            # Cache for 6 hours
            self.cache[asin] = result
            
            logger.info(f"[KEEPA] Validated {asin}: {drop_percent}% drop from €{avg90:.2f}")
            
            return result

        except Exception as e:
            logger.error(f"[KEEPA] Validation error for {asin}: {e}")
            return None
