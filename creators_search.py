import os
import requests
import time
from collections import deque

# ---------------------------------------------------------------------------
# Cognito v2 token endpoints
# ---------------------------------------------------------------------------
V2_TOKEN_ENDPOINTS = [
    "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token",
    "https://creatorsapi.auth.eu-south-2.amazoncognito.com/oauth2/token",
    "https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token",
]

# ---------------------------------------------------------------------------
# Deal keywords per marketplace language.
# These surface discounted / promotional listings better than generic "deal".
# Rotated automatically across calls so you don't always hit the same results.
# ---------------------------------------------------------------------------
DEAL_KEYWORDS_BY_MARKETPLACE = {
    "DE": ["Angebot", "Aktion", "Bestseller", "Schnäppchen", "Rabatt"],
    "GB": ["deal", "sale", "clearance", "offer", "bargain"],
    "IT": ["offerta", "saldi", "sconto", "promozione"],
    "FR": ["offre", "soldes", "promo", "réduction"],
    "ES": ["oferta", "rebajas", "descuento", "promoción"],
}

# ---------------------------------------------------------------------------
# High-value search indices for deal hunting.
# "All" spreads results too thin; targeted indices surface deals more reliably.
# Pass one of these as search_index, or cycle through them externally.
# ---------------------------------------------------------------------------
DEAL_SEARCH_INDICES = [
    "Electronics",
    "HomeAndKitchen",
    "Apparel",
    "SportsAndOutdoors",
    "Toys",
    "HealthPersonalCare",
    "Tools",
    "VideoGames",
    "Beauty",
    "Automotive",
]

# Internal sentinel returned by _try_request to signal a partner-tag error
_TAG_ERROR = object()


class CreatorsSearch:
    """
    Amazon Creators API SearchItems wrapper optimised for deal hunting.

    Changes vs original:
    - Token cached for ~1 h (avoids an auth round-trip on every call)
    - Single canonical camelCase payload (no more 5-6 variants + no-tag copies)
    - SortBy defaults to "Price:LowToHigh" (better for deals than "Featured")
    - Adaptive rate limiting with sliding window + 429 exponential backoff
    - Keyword rotation through locale-specific deal terms
    - search_all_pages() with ASIN deduplication
    - Results always normalised to a consistent PascalCase shape
    - Trimmed resource list (removed ancestor BrowseNode + redundant Condition)
    - Removed undocumented / empty headers that may confuse strict gateways
    - deliveryFlags: ["PRIME"] added — Prime listings have more reliable deals
    """

    def __init__(self, config):
        self.config = config
        self.marketplace = config.get("amazon", {}).get("marketplace", "DE")
        self._load_credentials()
        self.basic_mode = bool((config.get("creators") or {}).get("basic_mode", False))

        # --- Token cache ---
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._token_version: str | None = None

        # --- Sliding-window rate limiter ---
        # Default: 1 request per second.  Tune via config scanner.creators_rate
        rate_cfg = (config.get("scanner") or {})
        self._rate_window: float = float(rate_cfg.get("creators_rate_window", 1.0))
        self._rate_max: int = int(rate_cfg.get("creators_rate_max", 1))
        self._request_times: deque = deque()

        # --- Keyword rotation state ---
        self._keyword_index: int = 0

        # --- API call counter ---
        self.api_calls: int = 0

        # Prefer catalog endpoint, then legacy variants
        self._search_endpoints = [
            "https://creatorsapi.amazon/catalog/v1/searchItems",
            "https://creatorsapi.amazon/search/v1/searchItems",
            "https://api.amazon.com/creators/searchitems",
        ]

    # =========================================================================
    # Credential loading
    # =========================================================================

    def _load_credentials(self):
        marketplace_key = f"Amazon_{self.marketplace}"
        creds = self.config.get(marketplace_key, {})

        def env_or_config(field: str, *generic_names: str) -> str | None:
            marketplace_env = f"{marketplace_key}_{field}".upper()
            marketplace_env = "".join(c if c.isalnum() else "_" for c in marketplace_env)
            for name in (marketplace_env, *generic_names):
                value = os.getenv(name)
                if value is not None and value.strip():
                    return value
            return creds.get(field)

        self.application = env_or_config(
            "Application",
            "CREATORS_APPLICATION",
            "AMAZON_CREATORS_APPLICATION",
        )
        self.application_id = env_or_config(
            "Application_Id",
            "CREATORS_APPLICATION_ID",
            "AMAZON_CREATORS_APPLICATION_ID",
        )
        self.credential_id = env_or_config(
            "Credential_Id",
            "CREATORS_CREDENTIAL_ID",
            "AMAZON_CREATORS_CREDENTIAL_ID",
        )
        self.secret = env_or_config(
            "Secret",
            "CREATORS_SECRET",
            "CREATORS_CLIENT_SECRET",
            "AMAZON_CREATORS_SECRET",
        )

    # =========================================================================
    # OAuth — token caching
    # =========================================================================

    def _get_token_v3(self, client_id: str, client_secret: str) -> str | None:
        try:
            resp = requests.post(
                "https://api.amazon.com/auth/o2/token",
                json={
                    "grant_type":    "client_credentials",
                    "client_id":     client_id,
                    "client_secret": client_secret,
                    "scope":         "creatorsapi::default",
                },
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if resp.status_code == 200:
                return resp.json().get("access_token")
        except Exception:
            pass
        return None

    def _get_token_v2(self, client_id: str, client_secret: str) -> str | None:
        body = (
            f"grant_type=client_credentials"
            f"&client_id={client_id}"
            f"&client_secret={client_secret}"
            f"&scope=creatorsapi/default"
        )
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        for url in V2_TOKEN_ENDPOINTS:
            try:
                resp = requests.post(url, data=body, headers=headers, timeout=30)
                if resp.status_code == 200:
                    return resp.json().get("access_token")
            except Exception:
                continue
        return None

    def _get_access_token(self) -> tuple[str, str]:
        """
        Return (token, version). Token is re-used until 60 s before expiry.
        This avoids an auth round-trip on every search call.
        """
        now = time.time()
        if self._token and now < self._token_expiry - 60:
            return self._token, self._token_version  # type: ignore[return-value]

        if not self.credential_id or not self.secret:
            raise RuntimeError(
                "Missing Amazon Creators credentials. Set "
                f"{self.marketplace} config values or env vars like "
                f"AMAZON_{self.marketplace}_CREDENTIAL_ID and AMAZON_{self.marketplace}_SECRET."
            )

        token = self._get_token_v3(self.credential_id, self.secret)
        if token:
            self._token, self._token_expiry, self._token_version = token, now + 3600, "v3"
            return token, "v3"

        token = self._get_token_v2(self.credential_id, self.secret)
        if token:
            self._token, self._token_expiry, self._token_version = token, now + 3600, "v2"
            return token, "v2"

        raise RuntimeError("Failed to obtain Creators API access token")

    # =========================================================================
    # Rate limiting — sliding window
    # =========================================================================

    def _rate_limit_wait(self) -> None:
        """Block until a request slot is available in the current window."""
        now = time.time()
        while self._request_times and self._request_times[0] < now - self._rate_window:
            self._request_times.popleft()
        if len(self._request_times) >= self._rate_max:
            wait = self._rate_window - (now - self._request_times[0])
            if wait > 0:
                time.sleep(wait)
        self._request_times.append(time.time())

    # =========================================================================
    # Resources
    # =========================================================================

    def _get_resources(self) -> list[str]:
        if self.basic_mode:
            return [
                "itemInfo.title",
                "images.primary.medium",
                "offersV2.listings.price",
                "offersV2.listings.merchantInfo",
                "offersV2.listings.isBuyBoxWinner",
            ]
        return [
            # Category context — used for downstream categorisation
            "browseNodeInfo.browseNodes",
            # Visual
            "images.primary.medium",
            # Core item info
            "itemInfo.title",
            "itemInfo.externalIds",
            "parentASIN",
            # Pricing — includes savingBasis for accurate % calculation
            "offersV2.listings.price",
            # Deal metadata — AccessType, PercentClaimed, EndTime (urgency signals)
            "offersV2.listings.dealDetails",
            # Merchant / fulfilment
            "offersV2.listings.merchantInfo",
            "offersV2.listings.isBuyBoxWinner",
            "offersV2.listings.type",
            # Stock status
            "offersV2.listings.availability",
            #
            # REMOVED vs original:
            #   browseNodeInfo.browseNodes.ancestor  — slow, not needed for filtering
            #   offersV2.listings.condition          — redundant when Condition="New"
        ]

    # =========================================================================
    # Payload builder — single canonical form (camelCase for catalog endpoint)
    # =========================================================================

    def _build_payload(
        self,
        page: int,
        search_index: str,
        sort_by: str,
        condition: str,
        availability: str,
        min_saving_percent: int,
        max_price: float,
        partner_tag: str | None,
        keywords: str,
    ) -> dict:
        """
        One canonical camelCase payload.  No more 5-6 variants.

        Key decisions:
        - maxPrice in cents (int) — the only variant the catalog endpoint accepts
        - deliveryFlags: ["PRIME"] — Prime listings surface better deals and are
          more reliably priced; remove this if you want third-party non-Prime results
        - keywords required by the API (at least one search term must be present)
        - sortBy "Price:LowToHigh" — combined with minSavingPercent this surfaces
          the cheapest genuinely-discounted items first
        """
        # deliveryFlags: read from config if present. By default do NOT send
        # the deliveryFlags parameter (leave it out) to avoid catalog enum issues.
        # Set config['amazon']['delivery_flags'] = "PRIME" to request Prime-only.
        df_cfg = self.config.get("amazon", {}).get("delivery_flags", "")
        if isinstance(df_cfg, str):
            df_list = [p.strip() for p in df_cfg.split(",") if p.strip()]
        elif isinstance(df_cfg, (list, tuple)):
            df_list = list(df_cfg)
        else:
            df_list = []

        payload: dict = {
            "marketplace":        f"www.amazon.{self._tld_for_marketplace(self.marketplace)}",
            "itemPage":           page,
            "searchIndex":        search_index,
            "sortBy":             sort_by,
            "condition":          condition,
            "availability":       availability,
            "minSavingPercent":   min_saving_percent,
            "maxPrice":           int(float(max_price) * 100),  # cents
            "keywords":           keywords,
            "resources":          self._get_resources(),
        }
        # Only include deliveryFlags when non-empty (empty list = API ignores param)
        if df_list:
            payload["deliveryFlags"] = df_list
        if partner_tag:
            payload["partnerTag"] = partner_tag
        return payload

    # =========================================================================
    # Normalization — always applied, always consistent PascalCase output
    # =========================================================================

    def _norm_get(self, obj, path, default=None):
        cur = obj
        try:
            for p in path:
                cur = cur.get(p) if isinstance(cur, dict) else None
                if cur is None:
                    return default
            return cur if cur is not None else default
        except Exception:
            return default

    def _normalize_item(self, item: dict) -> dict:
        """
        Normalize a camelCase catalog item (or already-PascalCase item) to a
        consistent PascalCase structure.  Always called — no more split paths.
        """
        if not isinstance(item, dict):
            return item

        asin = item.get("asin") or item.get("ASIN")

        # Title can appear as displayValue, value, or a flat string in itemInfo/ItemInfo
        title = (
            self._norm_get(item, ["itemInfo",  "title", "displayValue"]) or
            self._norm_get(item, ["ItemInfo",  "Title", "DisplayValue"]) or
            self._norm_get(item, ["itemInfo",  "title", "value"]) or
            self._norm_get(item, ["ItemInfo",  "Title", "Value"]) or
            (self._norm_get(item, ["itemInfo",  "title"]) if isinstance(self._norm_get(item, ["itemInfo",  "title"]), str) else None) or
            (self._norm_get(item, ["ItemInfo",  "Title"]) if isinstance(self._norm_get(item, ["ItemInfo",  "Title"]), str) else None) or
            item.get("title") or item.get("Title") or ""
        )
        img_url = (
            self._norm_get(item, ["images",  "primary", "medium", "url"]) or
            self._norm_get(item, ["Images",  "Primary", "Medium", "URL"])
        )

        listings_src = (
            self._norm_get(item, ["offersV2", "listings"], []) or
            self._norm_get(item, ["OffersV2", "Listings"], []) or []
        )

        norm_listings = []
        for lst in listings_src:
            price_amt = (
                self._norm_get(lst, ["price",  "money",  "amount"]) or
                self._norm_get(lst, ["price",  "amount"]) or
                self._norm_get(lst, ["Price",  "Money",  "Amount"]) or
                self._norm_get(lst, ["Price",  "Amount"])
            )

            # savingBasis = crossed-out "was" price.
            # In OffersV2 camelCase responses this sits at the LISTING level,
            # not nested inside price.  Check all known structural variants:
            #   1. lst.savingBasis.amount             (listing-level, flat)
            #   2. lst.savingBasis.money.amount       (listing-level, money-nested)
            #   3. lst.price.savingBasis.amount       (inside price, flat)
            #   4. lst.price.savingBasis.money.amount (inside price, money-nested)
            #   5-8. PascalCase equivalents
            sb_amount = (
                self._norm_get(lst, ["savingBasis",  "amount"]) or
                self._norm_get(lst, ["savingBasis",  "money", "amount"]) or
                self._norm_get(lst, ["price", "savingBasis", "amount"]) or
                self._norm_get(lst, ["price", "savingBasis", "money", "amount"]) or

                # Additional fallbacks: savings object may directly contain the previous price
                self._norm_get(lst, ["price", "savings", "amount"]) or
                self._norm_get(lst, ["price", "savings", "money", "amount"]) or
                self._norm_get(lst, ["savings", "amount"]) or
                self._norm_get(lst, ["savings", "money", "amount"]) or

                # Flat PascalCase amount on the Price object
                self._norm_get(lst, ["Price", "SavingBasisAmount"]) or
                self._norm_get(lst, ["price", "savingBasisAmount"]) or
                self._norm_get(lst, ["SavingBasis",  "Amount"]) or
                self._norm_get(lst, ["SavingBasis",  "Money", "Amount"]) or
                self._norm_get(lst, ["Price", "SavingBasis", "Amount"]) or
                self._norm_get(lst, ["Price", "SavingBasis", "Money", "Amount"])
            )

            # savings.percentage: also try listing-level (not just inside price)
            savings_pct = None
            try:
                if sb_amount and price_amt and float(sb_amount) > 0:
                    savings_pct = int(round(
                        (float(sb_amount) - float(price_amt)) / float(sb_amount) * 100
                    ))
            except Exception:
                pass
            if savings_pct is None:
                savings_pct = (
                    self._norm_get(lst, ["price",       "savings",          "percentage"]) or
                    self._norm_get(lst, ["savings",                         "percentage"]) or
                    self._norm_get(lst, ["Price",       "Savings",          "Percentage"]) or
                    self._norm_get(lst, ["Savings",                         "Percentage"]) or
                    self._norm_get(lst, ["dealDetails", "savingsPercentage"]) or
                    self._norm_get(lst, ["DealDetails", "SavingsPercentage"])
                )

            # If still None, some PascalCase responses put the percent directly on root as SavingBasis: 27
            if savings_pct is None:
                sb_percent = self._norm_get(lst, ["SavingBasis"]) or self._norm_get(lst, ["savingBasis"])
                if isinstance(sb_percent, (int, float)) and 0 < sb_percent < 100:
                    try:
                        savings_pct = int(round(float(sb_percent)))
                    except Exception:
                        pass

            # Debug: if we still can't find savings, log the raw listing keys
            # so the path mismatch can be diagnosed from logs.
            if savings_pct is None and price_amt is not None:
                top_keys = list(lst.keys()) if isinstance(lst, dict) else "?"
                price_obj = (lst.get("price") or lst.get("Price") or {}) if isinstance(lst, dict) else {}
                price_keys = list(price_obj.keys()) if isinstance(price_obj, dict) else "?"
                print(f"[CREATORS] WARN savings_pct=None for price={price_amt} "
                      f"listing_keys={top_keys} price_keys={price_keys}")
                try:
                    import json as _json
                    print(_json.dumps(lst, indent=2)[:5000])
                except Exception:
                    pass

            # Full DealDetails preserved for downstream scoring:
            # AccessType (ALL / PRIME_EXCLUSIVE / PRIME_EARLY_ACCESS)
            # PercentClaimed — urgency signal (high = almost gone)
            # EndTime — show countdown
            deal_details = (
                self._norm_get(lst, ["dealDetails"]) or
                self._norm_get(lst, ["DealDetails"])
            )

            norm_listings.append({
                "Price": {
                    "Amount":            price_amt,
                    "SavingBasisAmount": sb_amount,
                },
                "MerchantInfo": {
                    "Id":   self._norm_get(lst, ["merchantInfo", "id"])   or self._norm_get(lst, ["MerchantInfo", "Id"]),
                    "Name": self._norm_get(lst, ["merchantInfo", "name"]) or self._norm_get(lst, ["MerchantInfo", "Name"]),
                },
                "SavingBasis":    savings_pct,
                "IsBuyBoxWinner": self._norm_get(lst, ["isBuyBoxWinner"]) or self._norm_get(lst, ["IsBuyBoxWinner"]) or False,
                "DealDetails":    deal_details,
            })

        browse_nodes = (
            self._norm_get(item, ["browseNodeInfo", "browseNodes"]) or
            self._norm_get(item, ["BrowseNodeInfo", "BrowseNodes"])
        )

        # Extract a single readable category string from the first browse node.
        # Tries displayName → name → id in both camelCase and PascalCase.
        category = None
        if isinstance(browse_nodes, list) and browse_nodes:
            first = browse_nodes[0]
            category = (
                self._norm_get(first, ["displayName"]) or
                self._norm_get(first, ["DisplayName"]) or
                self._norm_get(first, ["name"]) or
                self._norm_get(first, ["Name"]) or
                str(self._norm_get(first, ["id"]) or self._norm_get(first, ["Id"]) or "Unknown")
            )
        elif isinstance(browse_nodes, dict):
            category = (
                browse_nodes.get("displayName") or
                browse_nodes.get("DisplayName") or
                browse_nodes.get("name") or
                browse_nodes.get("Name") or
                "Unknown"
            )
        if not category:
            category = "Unknown"

        return {
            "ASIN":           asin,
            "ItemInfo":       {"Title": {"DisplayValue": title}},
            "Images":         {"Primary": {"Medium": {"URL": img_url}}},
            "OffersV2":       {"Listings": norm_listings},
            "BrowseNodeInfo": {"BrowseNodes": browse_nodes},
            "Category":       category,
            "ParentASIN":     item.get("parentASIN") or item.get("ParentASIN"),
        }

    # =========================================================================
    # Keyword rotation
    # =========================================================================

    def _next_keyword(self) -> str:
        """
        Rotate through locale-specific deal keywords.
        Config `amazon.keywords` overrides rotation when set to a non-generic value.
        """
        cfg_kw = (self.config.get("amazon") or {}).get("keywords", "")
        if cfg_kw and cfg_kw.lower() not in ("deal", ""):
            return cfg_kw
        kw_list = DEAL_KEYWORDS_BY_MARKETPLACE.get(self.marketplace, ["deal"])
        kw = kw_list[self._keyword_index % len(kw_list)]
        self._keyword_index += 1
        return kw

    # =========================================================================
    # Partner tag resolution
    # =========================================================================

    def _resolve_partner_tag(self) -> str | None:
        aff  = self.config.get("affiliate_ids") or {}
        code = str(self.marketplace).strip().upper()
        tag  = aff.get(code)
        if not tag and code == "GB":
            tag = aff.get("UK")
        if not tag:
            tag = (self.config.get("creators") or {}).get("partner_tag")
        if not tag and self.application_id:
            try:
                tag = str(self.application_id).split(".")[0]
            except Exception:
                pass
        return tag

    # =========================================================================
    # HTTP helpers
    # =========================================================================

    def _try_request(self, url: str, payload: dict, headers: dict) -> list | object | None:
        """
        POST payload to url.

        Returns:
          list   — items on success (may be empty-list → try next endpoint)
          _TAG_ERROR sentinel — 400 caused by invalid partner tag
          None   — any other failure → try next endpoint
        """
        backoff = 2.0
        for attempt in range(3):
            try:
                self.api_calls += 1
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
            except Exception as e:
                print(f"[CREATORS] Request exception {url}: {e}")
                return None

            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    print(f"[CREATORS] Invalid JSON from {url}: {resp.text[:200]}")
                    return None
                items = (
                    data.get("Items") or
                    data.get("items") or
                    (data.get("searchResult") or {}).get("items") or
                    []
                )
                if items:
                    print(f"[CREATORS] {len(items)} item(s) from {url}")
                else:
                    print(f"[CREATORS] 200 but no items from {url}. Keys: {list(data.keys())}")
                return items  # may be [] — caller decides whether to try next endpoint

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", backoff))
                print(f"[CREATORS] 429 at {url}. Waiting {retry_after:.1f}s (attempt {attempt + 1}/3)")
                time.sleep(retry_after)
                backoff *= 2
                continue

            if resp.status_code == 400:
                try:
                    body = resp.json()
                except Exception:
                    body = {"text": resp.text}
                if "partner tag" in str(body).lower() or "partnertag" in str(body).lower():
                    return _TAG_ERROR
                print(f"[CREATORS] 400 at {url}: {str(body)[:200]}")
                return None

            print(f"[CREATORS] HTTP {resp.status_code} at {url}: {resp.text[:200]}")
            return None

        return None

    # =========================================================================
    # Core search
    # =========================================================================

    def search_items(
        self,
        page: int = 1,
        search_index: str = "Electronics",
        sort_by: str = "Price:LowToHigh",
        min_saving_percent: int = 50,
        max_price: float = 450.0,
        keywords: str | None = None,
    ) -> list[dict]:
        """
        Search Amazon via Creators API.

        - Token cached (~1 h) — no auth overhead per call
        - Adaptive rate limiting — no fixed sleep
        - Single payload, no cascading variant retries
        - Results always normalised to consistent PascalCase

        API parameter notes:
        - sortBy "Price:LowToHigh" + minSavingPercent gives the cheapest
          genuinely-discounted items first (better than "Featured" for deals)
        - deliveryFlags: ["PRIME"] is set in the payload — removes junk
          third-party listings that rarely honour advertised discounts
        - maxPrice is converted to cents internally (API requirement)
        - keywords must be non-empty (API requirement); rotated automatically
        """
        self._rate_limit_wait()

        cfg_amz = self.config.get("amazon") or {}

        # Apply config overrides only when caller is using method defaults
        if search_index == "Electronics" and cfg_amz.get("search_index"):
            search_index = str(cfg_amz["search_index"]).strip()
        if self.basic_mode:
            search_index = "All"

        if sort_by == "Price:LowToHigh" and cfg_amz.get("sort_by"):
            sort_by = str(cfg_amz["sort_by"]).strip()

        condition    = cfg_amz.get("condition",    "New")
        availability = cfg_amz.get("availability", "Available")

        if min_saving_percent == 50 and cfg_amz.get("min_saving_percent") is not None:
            try:
                min_saving_percent = int(cfg_amz["min_saving_percent"])
            except Exception:
                pass

        if max_price == 450.0 and cfg_amz.get("max_price") is not None:
            try:
                max_price = float(cfg_amz["max_price"])
            except Exception:
                pass

        if self.basic_mode:
            min_saving_percent = min(min_saving_percent, 5)
            max_price          = max(max_price, 10_000.0)

        if not keywords:
            keywords = self._next_keyword()

        partner_tag = self._resolve_partner_tag()

        payload        = self._build_payload(page, search_index, sort_by, condition,
                                             availability, min_saving_percent, max_price,
                                             partner_tag, keywords)
        payload_no_tag = {k: v for k, v in payload.items() if k != "partnerTag"}

        try:
            token, cred_version = self._get_access_token()
        except Exception as e:
            print(f"[CREATORS] Auth error: {e}")
            return []

        # -------------------------------------------------------------------
        # Headers — only what the API actually uses.
        #
        # REMOVED vs original (undocumented / sent with empty values):
        #   x-marketplace        — not in Creators API docs
        #   X-Application-Id     — not in Creators API docs; empty string harmful
        #   X-Credential-Id      — not in Creators API docs
        #   X-Application        — not in Creators API docs
        #
        # KEPT:
        #   Authorization        — required
        #   Content-Type         — required
        #   Accept               — good practice
        #   User-Agent           — useful for Amazon's logging
        #   X-Amz-Auth-Version   — kept for v2 credential compat only
        # -------------------------------------------------------------------
        headers: dict = {
            "Authorization": f"Bearer {token}",
            # Some Creators endpoints (catalog) require explicit marketplace header
            "x-marketplace": f"www.amazon.{self._tld_for_marketplace(self.marketplace)}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "User-Agent":    "CreatorsDealFinder/1.0",
        }
        if cred_version == "v2":
            headers["X-Amz-Auth-Version"] = "2.1"

        for url in self._search_endpoints:
            result = self._try_request(url, payload, headers)

            if result is _TAG_ERROR:
                print(f"[CREATORS] Partner tag rejected at {url}, retrying without tag")
                result = self._try_request(url, payload_no_tag, headers)

            if isinstance(result, list):
                if result:
                    return [self._normalize_item(i) for i in result]
                # Empty list = 200 but no items; try next endpoint
                continue

            # None = hard failure; try next endpoint
        return []

    # =========================================================================
    # Multi-page search with deduplication
    # =========================================================================

    def search_all_pages(self, max_pages: int = 10, **kwargs) -> list[dict]:
        """
        Paginate search_items() and deduplicate by ASIN.
        Stops early if a page returns nothing.

        Example:
            all_deals = cs.search_all_pages(
                max_pages=5,
                search_index="Electronics",
                min_saving_percent=40,
            )
        """
        results:     list[dict] = []
        seen_asins:  set[str]   = set()

        for page in range(1, max_pages + 1):
            items = self.search_items(page=page, **kwargs)
            if not items:
                print(f"[CREATORS] No items on page {page}, stopping pagination")
                break

            new_items = []
            for item in items:
                asin = item.get("ASIN") or item.get("asin")
                if asin and asin not in seen_asins:
                    seen_asins.add(asin)
                    new_items.append(item)

            results.extend(new_items)
            print(f"[CREATORS] Page {page}: {len(new_items)} new ({len(results)} total, {len(items) - len(new_items)} dupes)")

        return results

    # =========================================================================
    # Diagnostics
    # =========================================================================

    def _probe_search(self) -> list[dict]:
        """Quick connectivity probe with permissive filters."""
        return self.search_items(
            page=1,
            search_index="Electronics",
            sort_by="Price:LowToHigh",
            min_saving_percent=5,
            max_price=10_000.0,
            keywords="electronics",
        )

    # =========================================================================
    # Helpers
    # =========================================================================

    def _debug_item(self, raw_item: dict) -> None:
        """
        Call this on the first raw item returned by the API to print exactly
        which keys are present at each level.  Use this if savings are still
        N/A after deploying the fix — it tells you the exact path to add.

        Usage:
            items = cs.search_items(...)
            # pass the RAW item before normalization by temporarily removing
            # the _normalize_item call in search_items, or call via:
            cs._debug_item(raw_item)
        """
        import json
        listings = (
            (raw_item.get("offersV2") or {}).get("listings") or
            (raw_item.get("OffersV2") or {}).get("Listings") or []
        )
        print("[DEBUG] Raw item top-level keys:", list(raw_item.keys()))
        for i, lst in enumerate(listings[:1]):  # only first listing
            print(f"[DEBUG] Listing[{i}] keys:", list(lst.keys()))
            price = lst.get("price") or lst.get("Price") or {}
            print(f"[DEBUG] Listing[{i}].price keys:", list(price.keys()))
            savings = price.get("savings") or price.get("Savings") or lst.get("savings") or {}
            print(f"[DEBUG] Listing[{i}].savings:", savings)
            sb = price.get("savingBasis") or lst.get("savingBasis") or lst.get("SavingBasis") or {}
            print(f"[DEBUG] Listing[{i}].savingBasis:", sb)
            dd = lst.get("dealDetails") or lst.get("DealDetails") or {}
            print(f"[DEBUG] Listing[{i}].dealDetails:", dd)

    def _tld_for_marketplace(self, marketplace: str) -> str:
        return {
            "DE": "de",
            "GB": "co.uk",
            "IT": "it",
            "FR": "fr",
            "ES": "es",
        }.get(marketplace, "de")
