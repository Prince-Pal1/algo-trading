# IC Markets cTrader Open API — setup walkthrough

Step-by-step to get real-time XAUUSD data + live paper trading on IC Markets cTrader demo. Written 2026-04-14, verified against openapi.ctrader.com and icmarkets.com global entity.

## Status tracker (tick as you go)

- [ ] **Part 1** — IC Markets cTrader Raw demo account opened
- [ ] **Part 2** — Spotware Open API application registered (status: Submitted)
- [ ] **Part 3** — Credentials obtained (client_id + client_secret in password manager)
- [ ] **Part 4** — Spotware KYC approval email received (app status: Active)
- [ ] **Part 5** — Access token minted via sandbox playground
- [ ] **Part 6** — `.env` file populated + preflight smoke test passing
- [ ] **Part 7** — G.3 Day 1 shadow mode started

---

## Part 1 — IC Markets cTrader Raw demo (5 minutes)

1. Go to **https://www.icmarkets.com/global/en/open-trading-account/demo** (the `.com/global/` path — NOT `.eu/`; the EU entity doesn't accept India).
2. Fill the form:
   | Field | Value | Why |
   |---|---|---|
   | First/Last name | Your real name | Required for later live conversion |
   | Email | Your email | Credentials + login come here |
   | Phone | +91xxxxxxxxxx | Required, real number |
   | Country | **India** | Routes to Seychelles entity |
   | Platform | **cTrader** | NOT MT4/MT5 — cTrader has Python API |
   | Account type | **Raw Spread** / **cTrader Raw** | 0.0 pip + $3 commission, cheapest |
   | Currency | **USD** | XAUUSD sizing is USD |
   | Leverage | 500:1 | Demo leverage irrelevant; backtest caps it |
   | Deposit | $10,000 | Matches backtest config |
3. Accept terms, submit.
4. Wait for **two emails** from IC Markets:
   - Welcome email → click confirm link
   - cTrader credentials email → contains:
     - **cTrader ID** (e.g., `abc1234567` or email-shaped)
     - **Account number** (7-8 digits, e.g., `5012345`)
     - **Server name** (e.g., `icmarkets-demo01`)
5. **Save these 3 values to a password manager** labeled "IC Markets cTrader demo". You'll need the account number in Part 6.
6. Sanity check: go to **https://ct.icmarkets.com/**, log in with your cTrader ID, verify $10,000 demo balance + XAUUSD in watchlist. If missing, contact IC Markets live chat.

---

## Part 2 — Register Spotware Open API application (10 minutes + KYC wait)

1. Go to **https://openapi.ctrader.com/**.
2. Log in with your cTrader ID (same one from Part 1).
3. Click **"I am developing a new product"** (NOT "app or website owner") → **"Register your app"**.
4. Fill the form with exactly these values:
   - **Application name**: `algo-trading-gold` (no third-party brand names allowed)
   - **Logo**: skip
   - **Description** (155 chars, under the 180 max):
     > Personal Python trading bot for XAUUSD scalping on my own cTrader Raw account. Single-user self-hosted, uses OpenApiPy for M5 candles + order placement.
   - **Redirect URL**: `http://localhost:8080/callback` (click "+ Add redirect URL")
   - **Contact name / surname / phone**: real + reachable
   - **Company name**: `Individual`
   - **Company website**: blank
   - **Scope**: ☑ **"Access your account and trade"** (NOT the broader scopes — they trigger stricter review)
5. Click **Save**. Status becomes **"Submitted"**.

---

## Part 3 — Credentials (immediate, before KYC)

1. On the Applications page, click **"Credentials"** next to `algo-trading-gold`.
2. Copy `client_id` + `client_secret`.
3. **Save to a password manager** — do NOT commit to git, do NOT paste in any shared chat, do NOT put in `.env` yet.
4. These credentials are useful only AFTER KYC approves the app — the sandbox playground still requires "Active" status for both read-only and trading scopes.

---

## Part 4 — Wait for Spotware KYC approval

- Typical timeline: **a few hours to 1-3 business days**.
- Status in `openapi.ctrader.com/apps` flips from **"Submitted"** → **"Active"**.
- You'll get an email.
- If no response after 3 business days, ping **@ctrader_open_api_support** on Telegram.

**While you wait**: nothing to do. The Python skeleton is already built. Every line of code that uses `client_id`/`client_secret`/`access_token` is unit-tested with mocks. Dropping credentials in will be the only change.

---

## Part 5 — Mint the access token (sandbox playground)

Once the app is **"Active"**:

1. Go to **https://openapi.ctrader.com/apps**.
2. Click **"Sandbox"** next to `algo-trading-gold`.
3. Scope selector → **"Account info and trading"** (NOT "Account info" — that's read-only).
4. Click **"Get token"**.
5. Spotware will redirect through an OAuth flow:
   - Log in with your cTrader ID (auto-filled if already logged in)
   - Select your IC Markets demo account from the list
   - Click **Authorize** / **Allow**
6. The next page displays:
   - **`accessToken`** — long string, treat like a password
   - **`refreshToken`** — long string
   - **`expiresIn`** — seconds (~30 days)
   - **`ctidTraderAccountId`** — internal ID for your IC Markets demo
7. Copy all 4 values to your password manager. Still do NOT paste them anywhere else.

---

## Part 6 — Populate `.env` + run preflight

1. In Terminal:
   ```bash
   cd /Users/prince/algo-trading-gold
   cp .env.example .env
   open -e .env
   ```
2. Paste values into `.env` (TextEdit will open it):
   ```env
   CTRADER_CLIENT_ID=<from Part 3>
   CTRADER_CLIENT_SECRET=<from Part 3>
   CTRADER_ACCESS_TOKEN=<from Part 5>
   CTRADER_REFRESH_TOKEN=<from Part 5>
   CTRADER_ACCOUNT_ID=<7-8 digit number from Part 1, IC Markets welcome email>
   CTRADER_ENVIRONMENT=demo
   ```
3. Save + close.
4. Verify `.env` is gitignored:
   ```bash
   git check-ignore .env
   ```
   Should print `.env`. If it prints nothing, STOP — don't commit yet. Check `.gitignore` has a `.env` line.
5. Run the preflight smoke test:
   ```bash
   PYTHONPATH=. python3 scripts/g3_preflight.py
   ```
   Expected output: `✅ PREFLIGHT PASSED — G.3 Day 1 is safe to start`. This runs the shadow pipeline against historical parquet data (not live yet), so it works even before the live connection test.
6. (Optional) Live connection smoke test against IC Markets servers:
   ```bash
   PYTHONPATH=. python3 -c "
   from src.data.feeds.icmarkets_feed import ICMarketsFeed, ICMarketsConfig
   cfg = ICMarketsConfig.from_env()
   feed = ICMarketsFeed(cfg, symbols=['XAUUSD'], timeframes=['5m'])
   feed.start()  # prints symbol catalog + first XAUUSD ticks
   "
   ```
   Ctrl-C to stop. If you see `icmarkets_spot` events with XAUUSD prices, the live connection works.

---

## Part 7 — Launch G.3 Day 1 shadow mode

With credentials in place + preflight passing, start the 30-day paper clock:

- **Days 1-7**: shadow mode, M3S shadow_mode=True, L=1 institutional only. No orders placed. The orchestrator logs every decision donchian_gold + vol_momentum_gold would have made against the live IC Markets feed. This is `ShadowOrchestrator` wired to `ICMarketsFeed` instead of `ParquetReplayFeed` — zero code changes, just a feed swap.
- **Days 8-14**: institutional L=5, paper orders actually sent to IC Markets demo
- **Days 15-21**: institutional L=10
- **Days 22-28**: institutional L=25
- **Days 29-30**: institutional full cap 80×

Kill conditions at every step per `ROADMAP.md § Phase G` and the master plan. See `~/.claude/plans/parallel-noodling-goblet.md § Paper clock (30 days)` for the full kill-condition matrix.

**Gate to live**: 30 days clean, 0 broker stop-outs, institutional DD < 5%, ≥100 institutional trades, positive expectancy.

---

## Troubleshooting

**"Account info and trading" radio is disabled in sandbox**
→ App is still in "Submitted" status. Wait for KYC approval email.

**OAuth flow doesn't show the IC Markets demo in the account list**
→ Your cTrader ID isn't linked to the IC Markets demo. Go to `connect.spotware.com` → Trading Accounts → Add Account → enter IC Markets account number + server name from Part 1.

**Preflight smoke test crashes with ImportError on `ctrader_open_api`**
→ Run `pip install ctrader-open-api service_identity`.

**`.env` is tracked by git when it shouldn't be**
→ Run `git rm --cached .env` then add `.env` to `.gitignore` if missing.

**Refresh token expired**
→ Go back to the sandbox playground, mint a new token with the same scope, update `.env`, restart the engine. Refresh tokens last ~30 days — set a calendar reminder for Day 25 of the paper clock.

**Spotware KYC > 3 business days**
→ Ping `@ctrader_open_api_support` on Telegram. Backup brokers: Tickmill Raw ($7.20/round-trip) or Pepperstone Razor ($8.70/round-trip). The Python skeleton is broker-agnostic — only `src/data/feeds/icmarkets_feed.py` + `src/execution/icmarkets_executor.py` + `.env` change.

---

## Related files

- `src/data/feeds/icmarkets_feed.py` — feed adapter (unit-tested with mocks)
- `src/execution/icmarkets_executor.py` — executor (unit-tested with mocks)
- `scripts/ctrader_token_helper.py` — one-shot OAuth helper (alternative to sandbox)
- `.env.example` — template with all required env vars
- `scripts/g3_preflight.py` — end-to-end smoke test that runs without live credentials
