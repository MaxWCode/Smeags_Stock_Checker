#!/usr/bin/env python3
"""One Piece TCG preorder stock monitor."""

from __future__ import annotations

import json
import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# GitHub Actions always runs from US IPs; Shopify serves ex-VAT prices to non-UK IPs.
# Apply UK VAT (20%) when running in CI so the displayed price matches the UK storefront.
_SHOPIFY_VAT = 1.2 if os.getenv("GITHUB_ACTIONS") == "true" else 1.0

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")

STATUS_FILE = Path("status.json")
PRODUCTS_FILE = Path("products.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Cookie": "_shopify_country=GB",  # hint; ignored by some stores (price fixed in fetch_shopify)
}

AVAILABLE    = "available"
PREORDER     = "preorder"
COMING_SOON  = "coming_soon"
SOLD_OUT     = "sold_out"
UNKNOWN      = "unknown"

# Order matters — higher entries win on first match
KEYWORD_MAP = [
    (PREORDER,    ["pre-order", "preorder", "pre order", "available to pre", "preorder now"]),
    (AVAILABLE,   ["add to cart", "add to basket", "buy now", "in stock", "add to bag", "add to trolley"]),
    (COMING_SOON, ["coming soon", "notify me when", "notify me when available",
                   "email when available", "pre-order stock will be available",
                   "release date", "available soon"]),
    (SOLD_OUT,    ["sold out", "out of stock", "unavailable", "out of stock - email"]),
]

STATUS_EMOJI  = {AVAILABLE: "✅", PREORDER: "🔔", COMING_SOON: "⏳", SOLD_OUT: "❌", UNKNOWN: "❓"}
STATUS_COLOR  = {AVAILABLE: 0x00C851, PREORDER: 0x33B5E5, COMING_SOON: 0xFF9800, SOLD_OUT: 0xFF4444, UNKNOWN: 0x888888}

# Shopify-based shops that support the /products/{handle}.json API
SHOPIFY_DOMAINS = {"totalcards.net"}


def get_domain(url: str) -> str:
    return urlparse(url).netloc.replace("www.", "")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def load_status() -> dict:
    if STATUS_FILE.exists():
        try:
            return json.loads(STATUS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_status(data: dict) -> None:
    STATUS_FILE.write_text(json.dumps(data, indent=2))


def load_products() -> list:
    if not PRODUCTS_FILE.exists():
        log.error("products.json not found")
        return []
    try:
        return json.loads(PRODUCTS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Failed to load products.json: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Status detection
# ---------------------------------------------------------------------------

def detect_status_from_text(text: str) -> str:
    text = text.lower()
    for status, keywords in KEYWORD_MAP:
        if any(kw in text for kw in keywords):
            return status
    return UNKNOWN


def _shopify_json_url(url: str) -> str | None:
    """Convert a Shopify product page URL to its .json API endpoint."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if "/products/" not in path:
        return None
    handle = path.split("/products/")[-1]
    return f"{parsed.scheme}://{parsed.netloc}/products/{handle}.json"


def _fetch_soup(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")
    except requests.RequestException as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return None


def _extract_name(soup: BeautifulSoup, hint: str = "") -> str:
    for sel in ["h1", ".product__title", ".product-title", '[itemprop="name"]', ".page-title"]:
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)
    return hint


def _extract_price(soup: BeautifulSoup) -> str | None:
    og = soup.find("meta", {"property": "og:price:amount"})
    if og and og.get("content"):
        try:
            return f"£{float(og['content']):.2f}"
        except ValueError:
            pass
    el = soup.select_one("[data-price]")
    if el:
        raw = el.get("data-price", "").strip()
        if raw:
            try:
                return f"£{float(raw):.2f}"
            except ValueError:
                pass
    for sel in [".product__price", '[itemprop="price"]', ".regular-price",
                ".price-item--regular", ".price-item--sale"]:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(strip=True)
            if txt:
                return txt
    return None


def _extract_status(soup: BeautifulSoup) -> str:
    """Detect status from HTML. Checks specific short elements to avoid nav pollution."""
    status_text = ""

    # Buttons and submit inputs — most reliable CTA signal
    for btn in soup.find_all("button"):
        status_text += " " + btn.get_text(" ", strip=True)
    for inp in soup.find_all("input", {"type": "submit"}):
        status_text += " " + inp.get("value", "")

    # Availability-specific elements — cap at 80 chars each to skip large parent wrappers
    # that accidentally have availability-related class names (e.g. nav containers)
    for sel in [".availability", ".stock-status", ".product-availability",
                ".badge", ".label", "[class*='sold-out']", "[class*='in-stock']"]:
        for el in soup.select(sel):
            txt = el.get_text(" ", strip=True)
            if len(txt) <= 80:
                status_text += " " + txt

    status = detect_status_from_text(status_text)
    if status != UNKNOWN:
        return status

    # Widen to main product area only if still unknown
    main = soup.select_one("main, #main, .main-content, .product-detail, article")
    area = main or soup.body or soup
    return detect_status_from_text(area.get_text(" ", strip=True))


def _unavail_subtype(soup: BeautifulSoup) -> str:
    """When Shopify API confirms unavailable, check HTML to distinguish coming_soon from sold_out."""
    # Total Cards: first span.text-nowrap inside a stock-label element is the product-level status.
    # Debug confirmed: "Sold out" for OP-11, "Coming Soon" for OP-16 and EB-05.
    # id is "stock-label-" (trailing dash), so use starts-with selector.
    el = soup.select_one("[id^='stock-label'] span.text-nowrap")
    if el:
        t = el.get_text(strip=True).lower()
        if "coming soon" in t:
            return COMING_SOON
        if "sold out" in t or "out of stock" in t:
            return SOLD_OUT

    # Total Cards: #stockLevels only appears for sold-out products (says "Out of Stock").
    el = soup.select_one("#stockLevels")
    if el:
        t = el.get_text(strip=True).lower()
        if "out of stock" in t or "sold out" in t:
            return SOLD_OUT
        if "coming soon" in t:
            return COMING_SOON

    # Scope to product form — cross-sell sections lower on page have their own buttons.
    product_area = (soup.select_one("form[action*='/cart'], product-form") or soup)
    for btn in product_area.find_all("button"):
        t = btn.get_text(strip=True).lower()
        if "coming soon" in t:
            return COMING_SOON
        if "sold out" in t:
            return SOLD_OUT

    # Badges — whole-page but coming_soon signal only.
    # Don't scan for sold_out — cross-sell cards pollute the page with SOLD OUT badges.
    for el in soup.select("[class*='badge'], [class*='label']"):
        t = el.get_text(strip=True).lower()
        if len(t) <= 40 and "coming soon" in t:
            return COMING_SOON

    return SOLD_OUT


def fetch_shopify(url: str) -> tuple:
    """Fetch product availability from Shopify JSON API. Returns (name, status|None, price|None)."""
    json_url = _shopify_json_url(url)
    if not json_url:
        return None, None, None
    try:
        r = requests.get(json_url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return None, None, None
        data = r.json().get("product", {})
        name = data.get("title", "")
        variants = data.get("variants", [])
        available = any(v.get("available", False) for v in variants)
        raw_price = next((v.get("price") for v in variants if v.get("price")), None)
        price = f"£{float(raw_price) * _SHOPIFY_VAT:.2f}" if raw_price else None
        if not available:
            return name, None, price   # caller uses HTML to distinguish coming_soon/sold_out
        tags = [t.lower() for t in data.get("tags", [])]
        if any("pre" in t for t in tags) or "pre-order" in name.lower():
            return name, PREORDER, price
        return name, AVAILABLE, price
    except Exception as exc:
        log.debug("Shopify JSON fetch failed for %s: %s", url, exc)
        return None, None, None


def fetch_html(url: str, hint_name: str = "") -> tuple:
    """Scrape HTML page for product name, status, and price."""
    soup = _fetch_soup(url)
    if soup is None:
        return hint_name or get_domain(url), UNKNOWN, None
    name = _extract_name(soup, hint_name) or get_domain(url)
    price = _extract_price(soup)
    status = _extract_status(soup)
    return name, status, price


def check_url(url: str, hint_name: str = "") -> tuple:
    """Return (name, status, price) for a product URL."""
    domain = get_domain(url)

    if domain in SHOPIFY_DOMAINS:
        api_name, api_status, api_price = fetch_shopify(url)
        # HTML still needed for unavailability subtype; API price * 1.2 is used for VAT accuracy
        soup = _fetch_soup(url)
        if soup is None:
            return api_name or hint_name or domain, api_status or UNKNOWN, api_price
        name = api_name or _extract_name(soup, hint_name) or domain
        price = api_price or _extract_price(soup)
        if api_status is not None:
            return name, api_status, price
        return name, _unavail_subtype(soup), price

    return fetch_html(url, hint_name)


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def send_discord(name: str, status: str, price: str | None, url: str, timestamp: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        log.warning("DISCORD_WEBHOOK_URL not set — skipping Discord alert")
        return

    embed = {
        "title": f"{STATUS_EMOJI.get(status, '')} {name}",
        "url": url,
        "color": STATUS_COLOR.get(status, 0x888888),
        "fields": [
            {"name": "Status", "value": status.replace("_", " ").title(), "inline": True},
            {"name": "Price",  "value": price or "Unknown",              "inline": True},
        ],
        "footer": {"text": f"Checked at {timestamp}"},
    }
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        r.raise_for_status()
        log.info("Discord alert sent for %s", name)
    except requests.RequestException as exc:
        log.error("Discord webhook failed: %s", exc)


def send_email(name: str, status: str, price: str | None, url: str, timestamp: str) -> None:
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_APP_PASSWORD]):
        log.warning("Email credentials incomplete — skipping email alert")
        return

    subject = f"One Piece stock alert: {name}"
    body = (
        f"Product: {name}\n"
        f"Status:  {status.replace('_', ' ').title()}\n"
        f"Price:   {price or 'Unknown'}\n"
        f"URL:     {url}\n"
        f"Time:    {timestamp}\n"
    )

    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info("Email alert sent for %s", name)
    except smtplib.SMTPException as exc:
        log.error("Email send failed: %s", exc)


def send_alerts(name: str, status: str, price: str | None, url: str, timestamp: str) -> None:
    send_discord(name, status, price, url, timestamp)
    send_email(name, status, price, url, timestamp)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    products = load_products()
    if not products:
        log.error("No products to monitor — check products.json")
        return

    stored = load_status()

    for product in products:
        url = product.get("url", "").strip()
        if not url:
            continue
        hint_name = product.get("name", "")

        log.info("Checking: %s", url)
        name, status, price = check_url(url, hint_name)
        timestamp = now_utc()

        prev = stored.get(url, {})
        prev_status = prev.get("status", "")

        if status != prev_status:
            if prev_status:
                log.info("Status changed: %s → %s  [%s]", prev_status, status, name)
                send_alerts(name, status, price, url, timestamp)
            else:
                log.info("First check — baseline recorded: %s is %s", name, status)
        elif status == AVAILABLE:
            # Repeat alert every run while available — keeps pinging the phone until bought
            log.info("Still available — repeat alert: %s", name)
            send_alerts(name, status, price, url, timestamp)
        else:
            log.info("No change: %s is %s", name, status)

        stored[url] = {
            "name": name,
            "status": status,
            "price": price,
            "last_checked": timestamp,
            "last_changed": timestamp if status != prev_status else prev.get("last_changed", timestamp),
        }

    save_status(stored)
    log.info("Done — status saved to %s", STATUS_FILE)


def test_alerts() -> None:
    """Fire a test alert through every configured channel to verify credentials."""
    timestamp = now_utc()
    name = "One Piece OP-16 The Time of Battle Booster Box"
    status = PREORDER
    price = "£94.95"
    url = "https://totalcards.net/collections/one-piece-pre-orders/products/one-piece-card-game-op-16-the-time-of-battle-booster-box-24-packs"
    log.info("Sending test alerts (no real status check performed)")
    send_alerts(name, status, price, url, timestamp)
    log.info("Test complete")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="One Piece TCG stock monitor")
    parser.add_argument("--test", action="store_true", help="Send a test alert without checking URLs")
    args = parser.parse_args()

    if args.test:
        test_alerts()
    else:
        main()
