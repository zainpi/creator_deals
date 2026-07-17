"""Shared config loading for the web app and the scanner worker.

Loads config.yml and applies environment-variable overrides so both the
gunicorn web process (app.py) and the background worker (worker.py) build an
identical config dict.
"""

import os
import logging

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config.yml"


def _upper_key(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in str(s)).upper()


def _env(*names: str):
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return v
    return None


def apply_env_overrides(cfg: dict) -> dict:
    cfg = cfg or {}

    wh = _env("DISCORD_WEBHOOK", "DISCORD_WEBHOOK_URL")
    if wh:
        cfg.setdefault("discord", {})["webhook_url"] = wh

    # Per-method, per-tier Discord channels for the A/B comparison engine
    # (Method 1 = subcategory browse nodes, Method 2 = parent category browse
    # node). Deals route by Keepa drop %: tier90 / tier70 / tier50 / rest,
    # with Keepa+AI rejects going to trash.
    _tier_env = {
        "tier90": "90",
        "tier70": "70",
        "tier50": "50",
        "rest": "REST",
        "trash": "TRASH",
    }
    for method_key, num, alias in (("method1", "1", "UNO"), ("method2", "2", "DOS")):
        for tier, suffix in _tier_env.items():
            v = _env(
                f"DISCORD_WEBHOOK_METHOD{num}_{suffix}",
                f"DISCORD_WEBHOOK_METHOD_{alias}_{suffix}",
            )
            if v:
                mw = cfg.setdefault("discord", {}).setdefault("method_webhooks", {})
                entry = mw.get(method_key)
                if not isinstance(entry, dict):
                    entry = {}
                    mw[method_key] = entry
                entry[tier] = v

    keepa_key = _env("KEEPA_API_KEY")
    if keepa_key:
        cfg.setdefault("keepa", {})["api_key"] = keepa_key
    keepa_enabled = _env("KEEPA_ENABLED")
    if keepa_enabled is not None:
        try:
            cfg.setdefault("keepa", {})["enabled"] = str(keepa_enabled).lower() in ("1", "true", "yes", "on")
        except Exception:
            pass

    apify = _env("APIFY_API_TOKEN")
    if apify:
        cfg["apify_api_token"] = apify

    for k, v in list(cfg.items()):
        try:
            if not (isinstance(k, str) and k.startswith("Amazon_")):
                continue
            section = cfg.get(k) or {}
            up = _upper_key(k)
            for field in ("Application", "Application_Id", "Credential_Id", "Secret"):
                env_name = f"{up}_{_upper_key(field)}"
                ev = _env(env_name)
                if ev:
                    section[field] = ev
            cfg[k] = section
        except Exception:
            continue

    return cfg


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Load config.yml and apply env overrides. Never raises — returns at least {}."""
    try:
        with open(path, "r") as f:
            cfg = yaml.safe_load(f) or {}
        cfg = apply_env_overrides(cfg)
        logger.info(f"[CONFIG] Loaded from {path}")
        return cfg
    except Exception as e:
        logger.error(f"[CONFIG] Failed to load {path}: {e}")
        return apply_env_overrides({})
