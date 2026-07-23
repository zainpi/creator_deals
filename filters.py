import logging
import math

logger = logging.getLogger(__name__)

# Default blocked categories (will be overridden by config if available)
BLOCKED_CATEGORIES = {
    "Books",
    "Audible",
    "Prime Video",
    "Gift Cards",
    "Amazon Fresh",
    "Grocery",
    "Digital Music",
    "Kindle Store",
}

def set_blocked_categories(config):
    """Update blocked categories from config."""
    global BLOCKED_CATEGORIES
    blocked = config.get("filters", {}).get("blocked_categories", [])
    if blocked:
        BLOCKED_CATEGORIES = set(blocked)
        logger.info(f"Blocked categories updated: {BLOCKED_CATEGORIES}")


class DealFilters:
    """Deal filtering and validation logic."""

    @staticmethod
    def absolute_drop_gate_applies(average_price, average_floor):
        """Fixed currency-drop gates are waived below the average-price floor."""
        return float(average_price) >= float(average_floor)

    @staticmethod
    def scan_limits(credential_count, requests_per_credential, max_pages):
        """Return the combined daily budget and safe start-to-start cadence."""
        daily_budget = max(1, int(credential_count)) * max(
            1, int(requests_per_credential)
        )
        tick_seconds = math.ceil(
            86400 * max(1, int(max_pages)) / daily_budget
        )
        return daily_budget, max(1, tick_seconds)
    
    @staticmethod
    def extract_category(item):
        """Extract category from item structure."""
        nodes = item.get("BrowseNodeInfo", {}).get("BrowseNodes", [])

        if not nodes:
            return "Unknown"

        return nodes[0].get("DisplayName", "Unknown")

    @staticmethod
    def category_allowed(category):
        """Check if category is allowed."""
        return category not in BLOCKED_CATEGORIES

    @staticmethod
    def extract_seller(listing):
        """Extract seller info from listing."""
        merchant = listing.get("MerchantInfo", {})

        return {
            "seller_name": merchant.get("Name"),
            "seller_id": merchant.get("Id")
        }

    @staticmethod
    def seller_allowed(seller_name):
        """Check if seller name is acceptable."""
        if not seller_name:
            return False

        bad_keywords = [
            "marketplace",
            "warehouse",
            "used",
            "import",
            "fulfilled",
        ]

        seller_name_lower = seller_name.lower()

        # Allow Amazon and verified sellers
        if "amazon" in seller_name_lower:
            return True
        
        # Reject if contains bad keywords
        if any(keyword in seller_name_lower for keyword in bad_keywords):
            return False
        
        return True

    @staticmethod
    def extract_price(listing):
        """Extract price from listing."""
        try:
            price = listing.get("Price", {})
            amount = price.get("Amount")
            
            if amount:
                return float(amount)
            return None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def extract_savings_percent(listing):
        """Extract savings percentage."""
        try:
            # If the normalized listing contains an explicit SavingBasis percent, return it.
            # Return None when not present so callers can distinguish "no data" vs "0%".
            savings = listing.get("SavingBasis")
            if savings is None:
                return None
            try:
                return int(savings)
            except Exception:
                return None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def extract_image(item):
        """Extract product image URL."""
        try:
            return item.get("Images", {}).get("Primary", {}).get("Medium", {}).get("URL")
        except Exception:
            return None

    @staticmethod
    def extract_title(item):
        """Extract product title."""
        try:
            return item.get("ItemInfo", {}).get("Title", {}).get("DisplayValue")
        except Exception:
            return None
