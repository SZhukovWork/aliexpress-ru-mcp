# aliexpress-ru-mcp

**English** · [Русский](README.ru.md)

> Buyer side: works with the public aliexpress.ru storefront; no account needed.

An [MCP](https://modelcontextprotocol.io) server that gives LLM agents live,
honestly-labelled data from [aliexpress.ru](https://aliexpress.ru), the Russian
AliExpress storefront: ruble prices per variant, coupons with the order total
they really need, delivery dates and costs to a Russian city, sellers, and
buyer reviews with real pagination.

## Why another AliExpress server

Most AliExpress scrapers either work with aliexpress.com (dollars, no Russian
delivery, no ruble coupons) or return numbers that are not what a buyer pays.
This one is built around not doing that:

| Pitfall | What this server does |
|---|---|
| "Price" taken from the card's min–max range over all variants — lower than what the card shows | Price of the variant the card pre-selects (or the one you ask for), plus every variant with its options, price and stock |
| A search listing shows the price of another variant than the card (observed: the 2-pack for 499 ₽ while the card opens on 1 piece for 299 ₽) | Every hit says which variant its price is for (`listing_sku_id`); `get_product` prices exactly that variant on request |
| A coupon subtracted from one item although it needs an order of 908 ₽ or more | Every coupon with its minimum order; only what the ordered quantity unlocks is applied (one store coupon + one order-total tier), plus the next threshold |
| Empty delivery: the ship-from country guessed as "RU", the API answers "no methods" | The delivery quote is requested exactly as the product page does; an empty answer is reported as "unknown", never as free |
| Delivery quoted for a different city than asked (the region lives in a cookie) | The region is set on every request, and the city the site echoes back is checked |
| A lot of 100 pieces compared with a single piece (the payload's `lot` flag stays false on lot cards) | Lot size read from the card's own unit text ("Цена за 1 лот (100 штук)") |
| `/item/1_XXXX.html` items fail with the bare id | `source_id` is handled everywhere and corrected automatically |
| "Sellers leave characteristics empty" — they are loaded by an async widget | Characteristics (and optionally the description) come from the same widget endpoint the page uses |
| Only the first page of review texts | Real review pagination, sort by lowest rating first, star / photo / "from Russia" filters |
| Deep search pages repeat earlier items | Pages are chained with the site's own continuation token |
| Anti-bot captcha, blocked sessions | Headless Chromium mints a session once; a blocked session is re-minted automatically; otherwise a clear error |

Every response carries `fetched_at`; price and delivery answers also say which
city they are for.

## Tools

| Tool | What it returns |
|---|---|
| `search_products(query, page, sort, price_min, price_max, limit, city)` | 20 items per page; sort `relevance` / `orders` / `price_asc` / `price_desc` / `newest`; `total_found`, `has_more`. Per item: listing price and pre-discount price with the variant they belong to (`listing_sku_id`), the listing's "with coupon" price, rating, orders, listing delivery estimate, number of variants, Choice and sponsored flags, seller, `item_id`, `source_id`, URL |
| `get_product(item_id, source_id, sku_id, quantity, city, include_description)` | Selected variant (price, pre-discount price, stock, options); all variants; all coupons with thresholds; what coupons give at `quantity` and the next threshold; Choice combo terms; delivery methods with dates and cost to the city; rating with star split; orders; seller (positive feedback %, followers, badges, orders shipped, store age); characteristics; buyer protection and returns |
| `get_reviews(item_id, source_id, sort, stars, with_photos, from_russia, with_follow_up, page, limit, include_aspects)` | Reviews with date, stars, text and original text, bought variant, photos, helpful votes, buyer country, seller reply, follow-up; the star split; AliExpress's aspect tags ("Яркий свет" — 16 mentions, 100 % positive) |
| `compare_products(items, city)` | Up to 10 items side by side: price, variant, price range, price after coupons for one unit, cheapest and fastest delivery, rating, reviews, orders, flags |

## Requirements

| | |
|---|---|
| Python | ≥ 3.10, with [uv](https://docs.astral.sh/uv/) (or pip) |
| Browser | Chromium via Playwright, downloaded automatically on first run (≈300 MB download, ≈650 MB on disk in `~/.cache/ms-playwright`, shared by all Playwright tools). Warms up the aliexpress.ru session for a few seconds on first use and again if the anti-bot blocks it; all data requests are plain HTTP |
| Docker | Not needed: Chromium runs as a child process of the server on the host |
| System libraries | Already present on desktop Linux, macOS and Windows. On a minimal Debian/Ubuntu server install them once (root): `uvx --from playwright playwright install --with-deps chromium` |
| Network | aliexpress.ru answered from a foreign IP in our tests; delivery quotes are always for the Russian city in `AE_CITY` |
| Memory | The browser takes ≈1 GB while it runs — ≈10 s of warm-up (measured on a cold start), then it is closed |
| Display | Not needed (`AE_HEADLESS=0` shows the window to solve a captcha by hand) |
| Tested on | Linux (CachyOS; Playwright uses its Ubuntu build there). macOS and Windows are supported by Playwright but untested |

## Install

Claude Code:

```bash
claude mcp add aliexpress -e AE_CITY=Москва -- uvx --from git+https://github.com/SZhukovWork/aliexpress-ru-mcp aliexpress-ru-mcp
```

Any MCP client (`claude_desktop_config.json`, `.mcp.json`, …):

```json
{
  "mcpServers": {
    "aliexpress": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/SZhukovWork/aliexpress-ru-mcp", "aliexpress-ru-mcp"],
      "env": {"AE_CITY": "Москва"}
    }
  }
}
```

From a checkout: `uv venv && uv pip install -e . && .venv/bin/aliexpress-ru-mcp`.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `AE_CITY` | `Москва` | City for prices and delivery quotes, as named on the site. Add the region for namesakes: `Кировск, Мурманская`. Every tool also takes a `city` argument |
| `AE_PROXY` | — | Proxy URL for both HTTP and the browser, e.g. `http://user:pass@host:3128` |
| `AE_MIN_INTERVAL` | `1.0` | Seconds between requests to aliexpress.ru. The site throttles bursts |
| `AE_CACHE_DIR` | `~/.cache/aliexpress-ru-mcp` | Anti-bot session (file mode 0600) and resolved cities |
| `AE_HEADLESS` | `1` | `0` opens a visible browser window when a session is minted — use it once if the site shows a captcha; solve it and the session is reused |

## What the numbers mean

- **`price_rub`** is the variant's price for an anonymous buyer right now
  ("цена сейчас" on the card); `price_before_discount_rub` is the crossed-out one.
  A card's variants can differ a lot in price — the answer always names the
  variant (`sku_id`, `options`).
- **Coupons** gate on the whole order subtotal. `with_coupons` applies only the
  coupons the given `quantity` unlocks; `next_threshold` tells how much more to
  order for a bigger one. Store coupons have to be claimed on the card (free,
  one click): `needs_claim`. `site_card_coupon_price_rub` is the card's own
  "X ₽ with coupon" figure; if it disagrees with the thresholds, the answer says
  so. `multi_piece` discounts (−2 % from 3 pieces…) are listed but not added to
  totals: how they stack with coupons is not verified.
- **Search prices** belong to the variant the listing shows (`listing_sku_id`);
  the card may pre-select a cheaper or dearer one — pass `listing_sku_id` as
  `sku_id` to `get_product` to price the same variant. The price window filters
  on AliExpress's own price field, so a shown price can fall outside it.
  **`listing_coupon_price_rub`** is what the listing advertises "с купоном";
  the coupon may need a bigger order.
- **Choice combo items** (`combo`): the card price and free delivery are
  advertised for combo-cart orders from a threshold (usually 1 000 ₽). Below it
  the answer warns that the order may cost more and gives the API's "buy now"
  price for the variant alone — not verified at checkout.
- **`delivery`** is the real quote for the city: methods, date range, cost,
  whether a passport is needed. No methods means "unknown", not "free".
  **`delivery_estimate`** in search results is the listing's summary
  (`cost_rub_from` = cheapest method, `days` = the listing's range).
- **Rating** covers the whole item (all variants). **`orders`** is the "купили"
  counter — purchases, not reviews.
- **Seller**: `positive_feedback_percent` is the seller rating shown on the card;
  `badges`: `brand` (certified brand representative), `official` (the seller's own
  brand), `top` (top store); `orders_shipped` with `orders_shipped_period` is the
  closest thing to the store's age the site shows.
- **`lot`** / **`lot_pieces`**: the card sells lots ("Цена за 1 лот (100 штук)"),
  so every price on it is per lot. `price_unit` repeats the card's unit text.
- **`stock`** is the exact number the API reports (the card caps it at "99+").
- **`fulfilment`**: `own_warehouse` — shipped from an AliExpress warehouse
  (Choice); `dropshipping` — shipped by the seller (observed values).
- Review texts written in another language are machine-translated by
  AliExpress; `original_text` holds the original.

## Limitations

- Unofficial: relies on the storefront's internal endpoints, which can change
  at any time. Parsers are isolated in `parse.py` and covered by tests on
  recorded responses.
- Anonymous only: a signed-in buyer may see personal prices and coupons
  (new-user coupons, loyalty). Checkout is the final authority on totals.
- Tested from a Russian IP. After many fresh sessions from one IP the anti-bot
  can show a captcha even to the browser: wait, or run once with `AE_HEADLESS=0`.
- Search price sorting and the price window use AliExpress's own price field;
  a shown price can fall outside the window (see "What the numbers mean").
- Reviews come 10 per request; the star filter takes one star value at a time.
- Read-only: no account, cart or orders (see Roadmap).

## Roadmap

- **Later: optional account mode** (off by default). Log in once in a visible
  browser window — credentials never pass through the MCP client — to see your
  personal prices, claimed coupons and exact delivery to your address.
- **Later: cart** — `add_to_cart` / `get_cart` on top of the account mode, to
  check coupon and combo totals exactly.
  Checkout, payment and address changes are deliberately out of scope.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
.venv/bin/pytest            # offline tests on recorded responses
.venv/bin/pytest -m live    # end-to-end over MCP stdio against live aliexpress.ru (Russian IP)
```

## Disclaimer

Not affiliated with AliExpress or Alibaba Group. Intended for personal price
research; respect aliexpress.ru's terms of use and keep request rates low.

License: MIT.
