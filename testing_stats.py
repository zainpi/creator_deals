import logging

logger = logging.getLogger(__name__)


class TestingStats:
    """Track discovery and filtering statistics."""
    
    def __init__(self):
        self.total_found = 0
        self.category_filtered = 0
        self.seller_filtered = 0
        self.keepa_passed = 0
        self.keepa_failed = 0
        self.ai_passed = 0
        self.ai_failed = 0
        self.discord_posted = 0
        self.scan_count = 0
        self.last_scan_time = None
        self.api_calls = 0

    def as_dict(self):
        """Return stats as dictionary."""
        return {
            "total_found": self.total_found,
            "category_filtered": self.category_filtered,
            "seller_filtered": self.seller_filtered,
            "keepa_passed": self.keepa_passed,
            "keepa_failed": self.keepa_failed,
            "ai_passed": self.ai_passed,
            "ai_failed": self.ai_failed,
            "discord_posted": self.discord_posted,
            "scan_count": self.scan_count,
            "last_scan_time": self.last_scan_time,
            "api_calls": self.api_calls,
        }

    def log_summary(self):
        """Log stats summary."""
        logger.info(f"""
[STATS] Scan #{self.scan_count}
  Found: {self.total_found}
  Category Filtered: {self.category_filtered}
  Seller Filtered: {self.seller_filtered}
  Keepa Passed: {self.keepa_passed} (Failed: {self.keepa_failed})
  AI Passed: {self.ai_passed} (Failed: {self.ai_failed})
  Posted: {self.discord_posted}
""")
