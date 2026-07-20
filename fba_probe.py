#!/usr/bin/env python3
"""
fba_probe.py — one-shot, READ-ONLY diagnostic for the FBA leak.

Why this exists
---------------
method_scanner runs a browse-node-only search with deliveryFlags=["FulfilledByAmazon"]
("FBA: ON"), yet a non-FBA item still posted. This probe asks the LIVE Creators API,
using the exact same request the method engine makes, and dumps what each offer
listing actually contains — so we can see:

  1. Whether any per-offer FBA / fulfillment field even EXISTS to gate on
     (PA-API 5's OffersV2 schema has none — `type` is only LIGHTNING_DEAL /
     SUBSCRIBE_AND_SAVE, and there is no IsAmazonFulfilled). This confirms it for
     the Creators API specifically.
  2. What MerchantInfo (seller Name/Id) looks like for the offers that come back —
     the only per-offer signal we may be able to use as an FBA proxy.
  3. Which (if any) extra "fulfillment" resource names the API will accept.

It is SAFE: read-only. No Discord posts, no DB writes, no config edits. It makes
roughly 9 cheap API calls total.

Run it from the project folder, in the same environment the worker uses
(so credentials / env vars resolve the same way):

    python3 fba_probe.py                       # default: DE, browse node 340843031
    python3 fba_probe.py --node 78191031       # a specific browse node (e.g. an Angebote node)
    python3 fba_probe.py --keyword Samsung      # a keyword search instead of a node
    python3 fba_probe.py --marketplace DE --node 340843031

Then paste the whole output back.
"""

import argparse
import json

from config_loader import load_config
from creators_search import CreatorsSearch

# Full set of *documented-valid* OffersV2 sub-resources (camelCase, as this
# codebase names them). None of these is a fulfillment flag — that's the point.
FULL_OFFERSV2 = [
    "itemInfo.title",
    "images.primary.medium",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "offersV2.listings.dealDetails",
    "offersV2.listings.isBuyBoxWinner",
    "offersV2.listings.loyaltyPoints",
    "offersV2.listings.merchantInfo",
    "offersV2.listings.price",
    "offersV2.listings.type",
    "offersV2.listings.violatesMAP",
]

# Candidate fulfillment resource names that are NOT in the documented OffersV2
# schema. We probe each ON ITS OWN so an "invalid resource" rejection can't
# affect the others. If the API accepts one, that's the field to gate on.
FULFILLMENT_CANDIDATES = [
    "offersV2.listings.deliveryInfo",
    "offersV2.listings.deliveryInfo.isAmazonFulfilled",
    "offersV2.listings.deliveryInfo.isPrimeEligible",
    "offersV2.listings.deliveryInfo.isFreeShippingEligible",
    "offersV2.listings.isFulfilledByAmazon",
    "offersV2.listings.fulfillmentType",
    "offersV2.listings.fulfillment",
    "offersV2.listings.programEligibility",
]


def make_search(config, marketplace):
    cs = CreatorsSearch(config)
    cs.marketplace = str(marketplace).upper()
    try:
        cs._load_credentials()
    except Exception as e:
        print(f"  (credential load note: {e})")
    return cs


def run(cs, resources, node, keyword, item_count=10):
    """Override the resource list for this one call, then run a single page with
    FBA ON — mirroring method_scanner's request."""
    cs._get_resources = lambda: resources  # per-call override (instance attribute)
    return cs.diagnostic_search(
        search_index="All",
        keywords=keyword or "",
        browse_node_id=node or None,
        sort_by="Price:LowToHigh",
        min_saving_percent=0,
        min_price=0.0,
        max_price=450.0,
        item_count=item_count,
        max_pages=1,
        use_keepa=False,
        delivery_flags=["FulfilledByAmazon"],  # FBA ON — same as the method engine
    )


def raw_items(raw):
    if not isinstance(raw, dict):
        return []
    return (raw.get("Items") or raw.get("items")
            or (raw.get("searchResult") or {}).get("items") or [])


def raw_listings(item):
    o = item.get("OffersV2") or item.get("offersV2") or {}
    return o.get("Listings") or o.get("listings") or []


def merchant_of(listing):
    m = listing.get("MerchantInfo") or listing.get("merchantInfo") or {}
    name = m.get("Name") or m.get("name")
    mid = m.get("Id") or m.get("id")
    return name, mid


def looks_amazon(name, mid):
    """True only when the SELLER is Amazon itself (sold-and-shipped-by-Amazon).
    Note: a 3P seller whose item is FBA would NOT match this — that's the gap."""
    if name and "amazon" in str(name).lower():
        return True
    # Known Amazon retail merchant ids per marketplace (best-effort; extend as needed)
    amazon_ids = {
        "ATVPDKIKX0DER",   # US
        "A3JWKAKR8XB7XF",  # DE
        "A1PA6795UKMFR9",  # DE (alt)
        "A3P5ROKL5A1OLE",  # ES
        "APJ6JRA9NG5V4",   # IT
        "A13V1IB3VYF5JG",  # FR
        "A1F83G8C2ARO7P",  # UK
    }
    return bool(mid and str(mid) in amazon_ids)


def main():
    ap = argparse.ArgumentParser(description="Read-only FBA leak probe (Creators API).")
    ap.add_argument("--marketplace", default="DE")
    ap.add_argument("--node", default="340843031", help="browse node id (ignored if --keyword given)")
    ap.add_argument("--keyword", default="", help="keyword search instead of a browse node")
    args = ap.parse_args()

    keyword = args.keyword.strip()
    node = "" if keyword else args.node.strip()

    print("=" * 78)
    print("FBA PROBE — read-only. FBA (deliveryFlags=FulfilledByAmazon) is ON for every call.")
    print(f"marketplace={args.marketplace}  " + (f"keyword={keyword!r}" if keyword else f"node={node}"))
    print("=" * 78)

    config = load_config()
    cs = make_search(config, args.marketplace)
    print(f"basic_mode={getattr(cs, 'basic_mode', '?')}  partner_tag={cs._resolve_partner_tag()!r}")

    # ---------------------------------------------------------------- baseline
    print("\n" + "-" * 78)
    print("STEP 1 — full valid OffersV2 dump (what the API actually returns per offer)")
    print("-" * 78)
    res = run(cs, FULL_OFFERSV2, node, keyword)
    resp = res.get("response") or {}
    sent_flags = ((res.get("request") or {}).get("payload") or {}).get("deliveryFlags")
    print(f"HTTP status: {resp.get('status')}   deliveryFlags actually sent: {sent_flags}")
    if res.get("error"):
        print(f"note: {res['error']}")

    items = raw_items(resp.get("raw"))
    print(f"items on page 1: {len(items)}")

    total, amazon_sold, third_party = 0, 0, 0
    shown = 0
    for it in items:
        asin = it.get("ASIN") or it.get("asin")
        listings = raw_listings(it)
        for lst in listings:
            total += 1
            name, mid = merchant_of(lst)
            is_amz = looks_amazon(name, mid)
            amazon_sold += int(is_amz)
            third_party += int(not is_amz)
            if shown < 5:
                shown += 1
                print(f"\n  ── ASIN {asin} · listing {shown} "
                      f"({'AMAZON-SOLD' if is_amz else 'NOT amazon-sold (possible non-FBA/3P)'})")
                print(f"     MerchantInfo: name={name!r} id={mid!r}")
                print(f"     listing keys: {sorted(lst.keys())}")
                print("     raw listing JSON:")
                print(json.dumps(lst, indent=2, ensure_ascii=False)[:2500])

    print("\n" + "-" * 78)
    print(f"SUMMARY: {total} offers returned with FBA ON  |  "
          f"amazon-SOLD={amazon_sold}  not-amazon-sold={third_party}")
    print("Your rule is 'shipper is Amazon' (= FBA). MerchantInfo above is the SELLER,")
    print("NOT the shipper — a 3P seller shipped BY Amazon (valid FBA) shows as")
    print("'not-amazon-sold' here. So MerchantInfo alone can't enforce your rule; we need")
    print("a real shipper/fulfillment field (Step 2) or a Keepa cross-check.")

    # ------------------------------------------------- candidate fulfillment fields
    print("\n" + "-" * 78)
    print("STEP 2 — does the Creators API accept any per-offer fulfillment resource?")
    print("-" * 78)
    accepted = []
    for cand in FULFILLMENT_CANDIDATES:
        r = run(cs, ["itemInfo.title", cand], node, keyword, item_count=1)
        st = (r.get("response") or {}).get("status")
        ok = st == 200
        accepted.append((cand, ok, st))
        tag = "ACCEPTED ✓" if ok else f"rejected ({st})"
        print(f"  {tag:16} {cand}")
        if not ok:
            raw = (r.get("response") or {}).get("raw")
            msg = json.dumps(raw, ensure_ascii=False)[:300] if raw is not None else ""
            if msg:
                print(f"                   -> {msg}")

    good = [c for c, ok, _ in accepted if ok]
    print("\n" + "=" * 78)
    if good:
        print("RESULT: the API accepts these fulfillment resource(s) — gate on one of them:")
        for g in good:
            print(f"   • {g}")
    else:
        print("RESULT: no fulfillment resource accepted. OffersV2 exposes NO per-offer")
        print("'shipper is Amazon' field. Options to enforce your rule then become:")
        print("  (a) Keepa cross-check — the app already calls Keepa; its buy-box/offer")
        print("      data flags whether the featured offer is FBA/Amazon-shipped.")
        print("  (b) Strict fallback — accept only offers SOLD by Amazon (MerchantInfo),")
        print("      which is always Amazon-shipped but drops legit 3P-FBA deals.")
        print("  (c) Keep deliveryFlags only (item-level) and accept occasional leaks.")
    print("=" * 78)


if __name__ == "__main__":
    main()
