#!/usr/bin/env python3
"""
Secret Lair Drop Monitor
Polls the Secret Lair store and Chaos Vault pages for new products,
then sends Discord webhook notifications when new items appear.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration (all overridable via environment variables)
# ---------------------------------------------------------------------------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "180"))  # 3 min default
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
)

# Pages to monitor
PAGES = {
    "store": "https://secretlair.wizards.com/us/",
    "chaos_vault": "https://secretlair.wizards.com/us/chaosvault",
}

# Optional: also monitor the "shop all" page which may list products not on the homepage
MONITOR_SHOP_ALL = os.environ.get("MONITOR_SHOP_ALL", "true").lower() == "true"
if MONITOR_SHOP_ALL:
    PAGES["shop_all"] = "https://secretlair.wizards.com/us/shopall"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("secret-lair-monitor")

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> dict:
    """Load known product IDs from the state file."""
    path = Path(STATE_FILE)
    if path.exists():
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.warning("Failed to read state file, starting fresh: %s", e)
    return {"known_products": {}, "last_check": None}


def save_state(state: dict) -> None:
    """Persist state to disk."""
    path = Path(STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

# Matches product links like /us/product/1249999 or /us/product/1249999/slug-name
PRODUCT_LINK_RE = re.compile(r"/us/product/(\d+)(?:/([^\"'\s]*))?")


def fetch_page(url: str) -> str | None:
    """Fetch a page's HTML. Returns None on failure."""
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        log.error("Failed to fetch %s: %s", url, e)
        send_discord_error_notification(str(e), context=f"Failed to fetch `{url}`")
        return None


def parse_products(html: str) -> dict[str, dict]:
    """
    Extract products from HTML.
    Returns dict keyed by product ID with metadata.
    """
    products: dict[str, dict] = {}
    soup = BeautifulSoup(html, "html.parser")

    # Find all anchor tags linking to product pages
    for anchor in soup.find_all("a", href=PRODUCT_LINK_RE):
        href = anchor.get("href", "")
        match = PRODUCT_LINK_RE.search(href)
        if not match:
            continue

        product_id = match.group(1)
        slug = match.group(2) or ""

        # Try to get the product name from the anchor's title attr or text content
        name = anchor.get("title", "").strip()
        if not name:
            # Look for heading tags inside the anchor's parent card
            parent = anchor.find_parent(class_=re.compile(r"product|card|item", re.I))
            if parent:
                heading = parent.find(re.compile(r"h[1-6]"))
                if heading:
                    name = heading.get_text(strip=True)
            if not name:
                name = anchor.get_text(strip=True)
        if not name:
            # Fall back to the slug
            name = slug.replace("-", " ").title() if slug else f"Product {product_id}"

        # Try to find price
        price = ""
        parent_card = anchor.find_parent(class_=re.compile(r"product|card|item", re.I))
        if parent_card:
            price_el = parent_card.find(string=re.compile(r"\$\d+"))
            if price_el:
                price_match = re.search(r"\$[\d,.]+", str(price_el))
                if price_match:
                    price = price_match.group(0)

        url = f"https://secretlair.wizards.com/us/product/{product_id}"
        if slug:
            url += f"/{slug}"

        products[product_id] = {
            "id": product_id,
            "name": name,
            "price": price,
            "url": url,
        }

    return products


def check_chaos_vault_active(html: str) -> bool:
    """
    Determine if the Chaos Vault currently has products listed
    (vs. just the 'GET NOTIFIED' placeholder page).
    """
    soup = BeautifulSoup(html, "html.parser")
    # If there are product links, the vault is active
    product_links = soup.find_all("a", href=PRODUCT_LINK_RE)
    return len(product_links) > 0


# ---------------------------------------------------------------------------
# Discord notifications
# ---------------------------------------------------------------------------

def send_discord_notification(products: list[dict], source: str) -> bool:
    """Send a Discord embed for new products."""
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping notification")
        return False

    # Color map for the embed sidebar
    colors = {
        "store": 0x7B2D8B,       # Purple for main store
        "chaos_vault": 0xE74C3C,  # Red for Chaos Vault
        "shop_all": 0x3498DB,     # Blue for shop all
    }

    source_labels = {
        "store": "Secret Lair Store",
        "chaos_vault": "Chaos Vault",
        "shop_all": "Shop All",
    }

    embeds = []
    for product in products:
        embed = {
            "title": product["name"],
            "url": product["url"],
            "color": colors.get(source, 0x95A5A6),
            "fields": [],
            "footer": {
                "text": f"Source: {source_labels.get(source, source)} • ID: {product['id']}",
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if product.get("price"):
            embed["fields"].append({"name": "Price", "value": product["price"], "inline": True})
        embed["fields"].append({"name": "Link", "value": f"[View Product]({product['url']})", "inline": True})
        embeds.append(embed)

    # Discord allows max 10 embeds per message. Batch if needed.
    for i in range(0, len(embeds), 10):
        batch = embeds[i : i + 10]
        payload = {
            "username": "Secret Lair Monitor",
            "avatar_url": "https://cdn-prod.scalefast.com/public/assets/img/resized/"
                          "wizardsofthecoast-secret-lair/favicon-32.png",
            "content": f"**🃏 New Secret Lair Drop{'s' if len(batch) > 1 else ''} Detected!**"
            if i == 0
            else None,
            "embeds": batch,
        }
        try:
            resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 5)
                log.warning("Rate limited by Discord, waiting %.1fs", retry_after)
                time.sleep(retry_after)
                resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
            resp.raise_for_status()
            log.info("Discord notification sent (%d embeds)", len(batch))
        except requests.RequestException as e:
            log.error("Failed to send Discord notification: %s", e)
            return False

    return True


def send_chaos_vault_opened_notification() -> bool:
    """Special notification when the Chaos Vault transitions from closed to open."""
    if not DISCORD_WEBHOOK_URL:
        return False

    payload = {
        "username": "Secret Lair Monitor",
        "avatar_url": "https://cdn-prod.scalefast.com/public/assets/img/resized/"
                      "wizardsofthecoast-secret-lair/favicon-32.png",
        "content": "# 🚨 CHAOS VAULT IS NOW OPEN! 🚨",
        "embeds": [
            {
                "title": "The Secret Lair Chaos Vault is LIVE",
                "url": "https://secretlair.wizards.com/us/chaosvault",
                "color": 0xE74C3C,
                "description": "The Chaos Vault has opened! Go check it out before it's gone!",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("Chaos Vault OPENED notification sent")
        return True
    except requests.RequestException as e:
        log.error("Failed to send Chaos Vault notification: %s", e)
        return False


# Error throttling: don't spam Discord if the same error keeps firing
_last_error_sent: dict[str, float] = {}
ERROR_NOTIFY_COOLDOWN = 900  # Only re-notify for the same error context every 15 minutes


def send_discord_error_notification(error_msg: str, context: str = "") -> bool:
    """Send an error notification to Discord so issues are visible even from phone."""
    if not DISCORD_WEBHOOK_URL:
        return False

    # Throttle: skip if we sent the same context recently
    now = time.time()
    throttle_key = context or error_msg[:100]
    if throttle_key in _last_error_sent:
        if now - _last_error_sent[throttle_key] < ERROR_NOTIFY_COOLDOWN:
            log.debug("Error notification throttled (cooldown): %s", throttle_key)
            return False

    description = f"```\n{error_msg[:1800]}\n```"
    if context:
        description = f"{context}\n{description}"

    payload = {
        "username": "Secret Lair Monitor",
        "avatar_url": "https://cdn-prod.scalefast.com/public/assets/img/resized/"
                      "wizardsofthecoast-secret-lair/favicon-32.png",
        "embeds": [
            {
                "title": "⚠️ Monitor Error",
                "color": 0xFFA500,  # Orange
                "description": description,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        resp.raise_for_status()
        _last_error_sent[throttle_key] = now
        return True
    except requests.RequestException:
        log.error("Failed to send error notification to Discord")
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_check(state: dict) -> dict:
    """Run one check cycle across all monitored pages."""
    known = state.get("known_products", {})
    chaos_vault_was_active = state.get("chaos_vault_active", False)

    for source, url in PAGES.items():
        log.debug("Checking %s: %s", source, url)
        html = fetch_page(url)
        if html is None:
            continue

        # Special Chaos Vault open/close detection
        if source == "chaos_vault":
            is_active = check_chaos_vault_active(html)
            if is_active and not chaos_vault_was_active:
                log.info("Chaos Vault has OPENED!")
                send_chaos_vault_opened_notification()
            elif not is_active and chaos_vault_was_active:
                log.info("Chaos Vault has closed.")
            state["chaos_vault_active"] = is_active

        products = parse_products(html)
        new_products = []

        for pid, product in products.items():
            if pid not in known:
                log.info("New product found: [%s] %s (%s)", pid, product["name"], source)
                new_products.append(product)
                known[pid] = {
                    **product,
                    "first_seen": datetime.now(timezone.utc).isoformat(),
                    "source": source,
                }

        if new_products:
            send_discord_notification(new_products, source)

    state["known_products"] = known
    state["last_check"] = datetime.now(timezone.utc).isoformat()
    return state


def main() -> None:
    if not DISCORD_WEBHOOK_URL:
        log.warning(
            "DISCORD_WEBHOOK_URL is not set. Notifications will be logged but not sent."
        )

    log.info("Secret Lair Monitor starting")
    log.info(
        "Monitoring %d page(s), polling every %ds",
        len(PAGES),
        POLL_INTERVAL_SECONDS,
    )
    log.info("State file: %s", STATE_FILE)

    state = load_state()
    log.info("Loaded state with %d known products", len(state.get("known_products", {})))

    # First run: populate state without notifying (unless --notify-on-start flag)
    first_run = len(state.get("known_products", {})) == 0
    if first_run:
        log.info("First run detected — populating initial product list (no notifications)")
        # Temporarily unset webhook to suppress first-run notifications
        saved_webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        notify_on_start = os.environ.get("NOTIFY_ON_START", "false").lower() == "true"
        if not notify_on_start:
            globals()["DISCORD_WEBHOOK_URL"] = ""

        state = run_check(state)
        save_state(state)

        if not notify_on_start:
            globals()["DISCORD_WEBHOOK_URL"] = saved_webhook

        log.info(
            "Initial scan complete: %d products catalogued",
            len(state.get("known_products", {})),
        )

    while True:
        try:
            state = run_check(state)
            save_state(state)
        except Exception as e:
            log.exception("Unhandled error during check cycle")
            send_discord_error_notification(
                f"{type(e).__name__}: {e}",
                context="Unhandled error during check cycle",
            )

        log.debug("Sleeping %ds until next check", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
