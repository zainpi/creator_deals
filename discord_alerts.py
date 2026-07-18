import logging

logger = logging.getLogger(__name__)

try:
    from discord_webhook import DiscordWebhook, DiscordEmbed
except ImportError:
    DiscordWebhook = None
    DiscordEmbed = None


class DiscordAlerts:
    """Send deal alerts to Discord."""

    def __init__(self, webhook_url):
        self.webhook_url = webhook_url
        if not DiscordWebhook:
            logger.warning("[DISCORD] discord-webhook not installed")

    def send(self, product, webhook_url=None):
        """Send product alert to Discord.

        `webhook_url` optionally overrides self.webhook_url for this send only —
        used to route a deal to a specific channel (e.g. the Method 1 / Method 2
        comparison channels) without needing a separate DiscordAlerts instance.
        """
        target_url = webhook_url or self.webhook_url

        if not target_url:
            logger.warning("[DISCORD] No webhook URL configured")
            return False

        if not DiscordWebhook:
            logger.warning("[DISCORD] discord-webhook not available")
            return False

        try:
            webhook = DiscordWebhook(url=target_url)

            # Which Keepa window the avg/drop came from (90d, or 30d fallback)
            window = product.get("keepa_window") or 90

            embed = DiscordEmbed(
                title=product.get("title", "Unknown")[:256],
                color="03b2f8"
            )

            embed.add_embed_field(
                name="🔗 ASIN",
                value=product.get("asin", "N/A"),
                inline=True
            )

            price_val = product.get("current_price")
            price_str = f"€{price_val:.2f}" if isinstance(price_val, (int, float)) else "N/A"
            embed.add_embed_field(
                name="💶 Price",
                value=price_str,
                inline=True
            )

            savings_val = product.get("savings_percent")
            savings_str = f"{int(savings_val)}%" if isinstance(savings_val, (int, float)) else "N/A"
            embed.add_embed_field(
                name="📊 Savings",
                value=savings_str,
                inline=True
            )

            drop_val = product.get("keepa_drop_percent")
            drop_str = f"{float(drop_val):.0f}%" if isinstance(drop_val, (int, float)) else "N/A"
            embed.add_embed_field(
                name=f"📉 {window}d Drop",
                value=drop_str,
                inline=True
            )

            # Keepa average price (90d, or 30d when no 90d data existed)
            avg90_val = product.get("keepa_avg_90")
            avg90_str = f"€{float(avg90_val):.2f}" if isinstance(avg90_val, (int, float)) else "N/A"
            embed.add_embed_field(
                name=f"📈 {window}d Avg",
                value=avg90_str,
                inline=True
            )

            # Keepa sales rank and estimated monthly sold (if available)
            rank_val = product.get("keepa_sales_rank")
            rank_str = f"{int(rank_val):,}" if isinstance(rank_val, (int, float)) else "N/A"
            sold_val = product.get("keepa_monthly_sold")
            sold_str = f"{int(sold_val):,}" if isinstance(sold_val, (int, float)) else "N/A"
            embed.add_embed_field(
                name="🏷️ Rank",
                value=rank_str,
                inline=True
            )
            embed.add_embed_field(
                name="📦 Monthly Sold",
                value=sold_str,
                inline=True
            )

            ai_val = product.get("ai_score")
            ai_str = f"{float(ai_val):.0f}/100" if isinstance(ai_val, (int, float)) else "N/A"
            embed.add_embed_field(
                name="⭐ AI Score",
                value=ai_str,
                inline=True
            )

            embed.add_embed_field(
                name="🏪 Seller",
                value=product.get("seller_name", "Unknown"),
                inline=False
            )

            embed.add_embed_field(
                name="📂 Category",
                value=product.get("category", "Unknown"),
                inline=False
            )

            image = product.get("image")
            if image:
                embed.set_thumbnail(url=image)

            tld_map = {"DE": "de", "GB": "co.uk", "UK": "co.uk", "FR": "fr", "IT": "it", "ES": "es"}
            mk = str(product.get("marketplace", "DE")).upper()
            tld = tld_map.get(mk, "de")
            embed.set_url(
                f"https://www.amazon.{tld}/dp/{product.get('asin', '')}"
            )

            webhook.add_embed(embed)
            result = webhook.execute()
            
            logger.info(f"[DISCORD] Sent alert for {product.get('asin')}")
            return True

        except Exception as e:
            logger.error(f"[DISCORD] Send failed: {e}")
            return False

    def send_trash(self, product, webhook_url):
        """Compact reject alert for the trash-method-* audit channels: title,
        ASIN, price, and why the deal was filtered out."""
        if not webhook_url or not DiscordWebhook:
            return False

        try:
            webhook = DiscordWebhook(url=webhook_url, rate_limit_retry=True)

            embed = DiscordEmbed(
                title=product.get("title", "Unknown")[:256],
                color="99aab5"
            )
            # Make the title a clickable Amazon product link
            asin = product.get("asin")
            if asin:
                tld_map = {"DE": "de", "GB": "co.uk", "UK": "co.uk",
                           "FR": "fr", "IT": "it", "ES": "es"}
                mk = str(product.get("marketplace", "DE")).upper()
                embed.set_url(f"https://www.amazon.{tld_map.get(mk, 'de')}/dp/{asin}")
            embed.add_embed_field(
                name="🔗 ASIN",
                value=product.get("asin", "N/A"),
                inline=True
            )
            price_val = product.get("current_price")
            price_str = f"€{price_val:.2f}" if isinstance(price_val, (int, float)) else "N/A"
            embed.add_embed_field(
                name="💶 Price",
                value=price_str,
                inline=True
            )
            embed.add_embed_field(
                name="🗑️ Rejected",
                value=product.get("reject_reason", "Unknown"),
                inline=False
            )

            webhook.add_embed(embed)
            webhook.execute()
            return True

        except Exception as e:
            logger.error(f"[DISCORD] Trash send failed: {e}")
            return False
