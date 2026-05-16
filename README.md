# One Piece TCG Stock Monitor

Monitors UK TCG shops every 10 minutes and sends Discord + email alerts the moment a product flips from sold out / coming soon to available or pre-order.

## Supported shops

| Shop | Method |
|------|--------|
| Total Cards | Shopify JSON API |
| Hammerhead TCG | Shopify JSON API |
| Magic Madhouse | HTML scraper |
| Zatu Games | HTML scraper |
| Wayland Games | HTML scraper |

Any other shop can be added to `products.json` — the HTML scraper works generically.

---

## Local setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
cd YOUR_REPO
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in:

| Variable | Where to get it |
|----------|-----------------|
| `DISCORD_WEBHOOK_URL` | Discord server → Server Settings → Integrations → Webhooks → New Webhook |
| `EMAIL_FROM` | Your Gmail address |
| `EMAIL_TO` | Destination address (can be the same) |
| `EMAIL_APP_PASSWORD` | [Google App Passwords](https://myaccount.google.com/apppasswords) — requires 2FA enabled; **not** your normal password |

### 3. Add products to monitor

Edit `products.json`:

```json
[
  {
    "url": "https://totalcards.net/collections/one-piece-pre-orders/products/...",
    "name": "OP-16 Booster Box (Total Cards)"
  },
  {
    "url": "https://zatugames.co.uk/products/...",
    "name": "OP-16 Booster Box (Zatu)"
  }
]
```

The `name` field is optional — if omitted the script reads the product title from the page.

### 4. Run

```bash
python monitor.py
```

On first run the script records a baseline and sends no alerts. On every subsequent run it alerts if the status changed.

---

## GitHub Actions (automated, free)

The workflow runs every 10 minutes using GitHub's free Actions minutes.

### 1. Push your repo to GitHub

```bash
git init
git add .
git commit -m "init"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### 2. Add secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Value |
|-------------|-------|
| `DISCORD_WEBHOOK_URL` | Your webhook URL |
| `EMAIL_FROM` | Your Gmail address |
| `EMAIL_TO` | Recipient address |
| `EMAIL_APP_PASSWORD` | Your Gmail App Password |

> If you only want Discord alerts and not email (or vice versa), just leave the unused secrets empty — the script skips whichever service isn't configured.

### 3. Enable Actions

GitHub Actions is enabled by default on new repos. The workflow runs automatically via the schedule, or you can trigger it manually from the **Actions** tab → **Run workflow**.

`status.json` is committed back to the repo after each run so state persists between runs.

---

## How it works

1. Reads `products.json` for URLs to check.
2. For Shopify shops, calls the `/products/{handle}.json` endpoint — fast and accurate.
3. For other shops, fetches the HTML page and scans buttons, availability labels, and badges for keywords.
4. Compares the detected status against `status.json` from the previous run.
5. If the status changed, sends a Discord embed and/or email.
6. Writes the updated status back to `status.json`.

### Status values

| Status | Meaning |
|--------|---------|
| `available` | Add to cart / buy now |
| `preorder` | Pre-order / available to pre-order |
| `sold_out` | Sold out / out of stock / coming soon |
| `unknown` | Could not determine — check the URL manually |

---

## Troubleshooting

**No alerts on first run** — this is intentional. The first run saves a baseline. Alerts fire on the next run if anything changed.

**`unknown` status** — the scraper couldn't find a clear signal. Open the URL in your browser and check whether the shop uses heavy JavaScript to render the add-to-cart button (common with some Magento shops). You may need to add a site-specific selector in `fetch_html()`.

**Gmail "Login denied"** — make sure you're using an App Password, not your regular Gmail password, and that 2FA is enabled on your Google account.
