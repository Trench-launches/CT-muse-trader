# VIGIL: an open, automated memecoin trader for Solana

VIGIL is an AI trading persona that runs in the open. It follows a smart-money scanner's calls on Solana
memecoins (pump.fun launches and the coins trading on PumpSwap, Raydium and other DEXs), checks each coin
onchain (who can still mint or freeze it, Token-2022 taxes, who holds the supply), has xAI's **Grok** write its
read on the coin, trades by fixed rules through **Jupiter** with hard risk limits nothing can override, journals
every decision in a tamper-evident log, and posts its trades (wins and losses) on X.
A one-page website shows the journal, scorecard and open positions live.

This repo is everything you need to run **your own copy**: the Python backend (`ct_agent/`) and the
static site (`web/`). You bring your own keys. Nothing in the repo talks to anyone else's deployment.

> [!WARNING]
> **This software can trade real money, and memecoins are extremely risky.** It starts in paper mode
> (simulated trades) and stays there unless you deliberately switch it to live. If you go live, use a
> brand-new wallet holding only what you can afford to lose completely. Bugs, bad trades, rug pulls,
> leaked keys and exchange or chain failures can all lose you everything in that wallet.
> Nothing here is financial advice. The software is provided as is, without warranty (see [LICENSE](LICENSE)).

## How it works

```
 smart-money scanner (Supabase) ──► caller / trader ──┐       ┌──► social ──► X API (posts, replies)
 Solana RPC (mints, holders) ─────► mint safety,      │       │      outbox: daily cap, idempotent,
 Jupiter (quotes, swaps, prices) ─► Jupiter route,    ▼       │      never blind-retries an ambiguous send
 DexScreener (pools, USD prices) ─► Grok's read ──► SQLite ◄──┴── exits (stop / take-profit / max-hold,
 news RSS / X reads / Twitch ──► research ──────► (append-only        one batched price call per tick)
 (budgeted, attributed)   verify leads onchain    hash-chained journal, token cache)
                                                     ▲
                                                     └── api (public reads, admin writes) ◄── web/ (static site)
```

Each box is a separate OS process, started together by `ctagent run-all` (or one container each with
docker compose), so an X outage can't stop trading and a crash in one worker can't take down the rest.

| Worker | What it does |
| --- | --- |
| `research` | Pulls news, X posts and Twitch chat; links mentions to coins only after verifying them onchain |
| `caller` / `trader` | Copies the scanner's Solana calls, checks they're tradeable, gets Grok's read, buys by rule (paper or live) |
| `exits` | Checks stop-loss, take-profit ladder and max-hold every few seconds |
| `social` | Writes and sends posts and replies through a rate-limited outbox |
| `launchpad` | Optional: launches coins for people who ask on X (off by default) |
| `api` | FastAPI: public read endpoints for the site, token-protected admin endpoints |

### Rules enforced in code

| Rule | Where |
| --- | --- |
| Solana calls only; mints are base58 and case-sensitive, never lowercased | `sources/scanner.py`, `chain/keys.py` |
| A coin is tradeable only if it's a real mint, nobody can mint or freeze it, no Token-2022 extension can take or tax holders' coins, and Jupiter can route it | `chain/token.py` `safety_blockers`, `sources/scanner.py` |
| Buy rule: +`CT_SCANNER_TRIGGER_PCT` from the call within `CT_SCANNER_WINDOW_HOURS` (or on the call); Grok never decides the trade | `strategy/engine.py` |
| Liquidity, volume, wash-trade and holder-concentration screen (curves, pools and burns don't count as holders) | `strategy/screen.py`, `chain/token.py` `holder_facts` |
| Addresses from chat, speech or posts are leads until verified on this chain | `sources/store.py` |
| Third-party text is fenced as untrusted in the Grok prompt | `brain/grok.py` |
| Grok cannot override gates, limits or the kill switch | `strategy/engine.py` |
| Paper first; paper trades labelled and never shown as onchain | `execution/paper.py`, `social/compose.py` |
| Live needs mode=live, a human review flag, a valid trading-only key, Solana mainnet, and a fresh Jupiter order whose price impact is inside `CT_MAX_SLIPPAGE_BPS` | `execution/live.py` |
| Position, exposure, daily-loss and daily-entry limits, and a kill switch | `execution/risk.py` |
| Dangerous settings refused at start-up (e.g. live with no admin token) | `safety.py` |
| Every fill, exit and failure journalled, append-only and hash-chained | `db.py`, `schema.sql` |

## Quick start (local, paper mode)

You need Python 3.12+. Nothing else is required to start; to have calls to trade you need a scanner feed
(`CT_SUPABASE_URL`/`CT_SUPABASE_KEY`, see "Keys" below).

```bash
git clone https://github.com/Trench-launches/vigil.git && cd vigil
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows: .venv\Scripts\pip install -e ".[dev]"
cp .env.example .env                     # then fill in what you have (see "Keys" below)
.venv/bin/ctagent check                  # tests each connection and prints a safety review
.venv/bin/ctagent run trader --once      # one trading cycle: fetch calls, screen, read, paper-buy
.venv/bin/ctagent run-all                # every worker + the API on http://127.0.0.1:8000/docs
.venv/bin/pytest                         # the test suite, fully offline
```

To make decisions and paper trades, add at least `CT_GROK_API_KEY`. To see the site against your local API:

```bash
python -m http.server 5173 -d web        # then open http://localhost:5173
```

(`web/config.js` points at `http://localhost:8000` by default, and the API allows `localhost:5173`.)

### With docker compose

```bash
cp .env.example .env                     # fill it in
docker compose up -d --build             # one container per worker, one shared data volume
docker compose logs -f trader
```

The API is published on `127.0.0.1:8000` only. Put a reverse proxy with TLS in front of it if you want to
expose it.

### Stopping trading

Any one of these stops new trades at once:

```bash
ctagent kill on "reason"
touch data/KILL
curl -X POST localhost:8000/admin/kill -H "authorization: Bearer $CT_API_ADMIN_TOKEN" \
     -H "content-type: application/json" -d '{"on":true,"reason":"manual stop"}'
```

## Configuration

All settings are environment variables prefixed `CT_`, read from the environment or a `.env` file.
[`.env.example`](.env.example) lists **every** setting with its default and marks the secrets. Safe defaults:

- `CT_TRADING_MODE=paper`: simulated trades only.
- `CT_X_POSTING_ENABLED=false`: posts are drafted and queued, never sent.
- `CT_LAUNCHPAD_ENABLED=false` and `CT_CHAT_ENABLED=false`: nothing that spends money for strangers.
- `CT_API_ADMIN_TOKEN` empty: every `/admin/*` endpoint answers 403.

At start-up (`run`, `run-all`, `api`, `live-test`) the settings are reviewed by `ct_agent/safety.py`.
Dangerous combinations stop the process, for example live mode without an admin token, an admin token under
32 characters in live mode, the same key for the trading and launchpad wallets, or a `.env` value that's
really a comment. Risky but legal ones are logged as `SAFETY:` warnings, such as a wallet key present while in
paper mode. `ctagent check` prints the same review.

### Keys: where to get each one

| What | Settings | Where |
| --- | --- | --- |
| Grok (the brain) | `CT_GROK_API_KEY` | [console.x.ai](https://console.x.ai), API Keys. Billed per use by xAI; separate from X developer keys. |
| X reads | `CT_X_BEARER_TOKEN` | [developer.x.com](https://developer.x.com): create a project and app, copy the Bearer token. X API access is paid. |
| X posting | `CT_X_API_KEY`, `CT_X_API_SECRET`, `CT_X_ACCESS_TOKEN`, `CT_X_ACCESS_SECRET` | Same app. Set user authentication to Read and write, then generate the Access Token and Secret **while signed in as the bot account**. Or use OAuth 2.0: `CT_X_CLIENT_ID`/`CT_X_CLIENT_SECRET`, then run `ctagent x-login` once. |
| Solana RPC | `CT_SOLANA_RPC_URL`, `CT_SOLANA_BACKUP_RPC_URL` | The public RPC works for testing. For production use a provider such as [Helius](https://dashboard.helius.dev) (free tier is enough to start; it also serves coin metadata); the URL then contains your key, so treat it as a secret. |
| Jupiter (swaps and prices) | `CT_JUPITER_API_KEY` | Free key at [developers.jup.ag](https://developers.jup.ag) (about 1 request a second). Needed before going live. |
| Smart-money scanner | `CT_SUPABASE_URL`, `CT_SUPABASE_KEY` | Your own scanner writing calls to Supabase (`alert_performance`, rows with `chain=solana`). It is the bot's only source of coins. |
| Launchpad (optional) | `CT_LAUNCHER_WALLET_PRIVATE_KEY`, `CT_PINATA_JWT` | Its own small wallet (~0.5 SOL) and a [Pinata](https://app.pinata.cloud) JWT for coin images. |
| Twitch (optional) | `CT_TWITCH_*` | [dev.twitch.tv/console](https://dev.twitch.tv/console). |
| Trading wallet (live only) | `CT_TRADING_WALLET_PRIVATE_KEY` | Make a **new** Solana wallet just for the bot (e.g. Phantom: export the base58 private key) and fund it with a small amount of SOL. See [SECURITY.md](SECURITY.md). |
| Admin token | `CT_API_ADMIN_TOKEN` | Make one: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

### Going live (real money)

Only after you've run on paper long enough to trust the results, and read `ct_agent/execution/live.py`:

1. Create a new Solana wallet for the bot only, and fund it with what you can afford to lose.
2. Set `CT_TRADING_WALLET_PRIVATE_KEY`, `CT_JUPITER_API_KEY`, a provider `CT_SOLANA_RPC_URL`, a 32+ character
   `CT_API_ADMIN_TOKEN`, `CT_LIVE_EXECUTION_REVIEWED=true` and finally `CT_TRADING_MODE=live`.
3. Run `ctagent check` (it should report the Solana RPC as mainnet, a Jupiter quote for 0.01 SOL to USDC,
   `live_trading: READY` and `safety: ok`), then `ctagent live-test <mint> --sol 0.01` for one tiny round trip
   (buy, then sell everything bought) and confirm both transactions on Solscan before letting the bot run.
4. Keep the limits small (`CT_MAX_POSITION_SOL`, `CT_MAX_TOTAL_EXPOSURE_SOL`, `CT_MAX_DAILY_LOSS_SOL`).

## Deploying

The backend is one long-running container (`Dockerfile`, `ctagent run-all`) with a persistent volume at
`/app/data`. The site is static files in `web/`. Any host that runs a container and serves static files works;
the repo has configs for Railway and Vercel.

### Backend on Railway (or any container host)

1. Create a project and a service from this repo; Railway reads `railway.json` and the `Dockerfile`.
2. Attach a volume mounted at `/app/data` (the database and journal live there).
3. Add your settings as service **Variables** (never commit them). At minimum `CT_GROK_API_KEY`,
   `CT_API_ADMIN_TOKEN` and `CT_SITE_ORIGINS=["https://your-site.example"]`.
4. `.railwayignore` keeps `.env`, data, tests and the site out of the upload.
5. Optional CI deploys: `.github/workflows/deploy-railway.yml` deploys on every push to `main` after the tests
   pass. Add a Railway **project token** as the `RAILWAY_TOKEN` repository secret, and your service name as
   the `RAILWAY_SERVICE` repository variable. Without the variable the deploy job is skipped.

### Site on Vercel (or any static host)

The site reads its settings from `web/config.js`, and link previews read a few `<head>` tags in
`web/index.html` (canonical URL, `og:*`, `twitter:*`), which ship with `example.com` placeholders.
`scripts/deploy-web.sh` fills in both from `SITE_*` variables in a temporary copy, so your values never need
committing. Put them in a gitignored `.env.site`:

```bash
SITE_URL=https://www.your-site.example          # required: the site's own address
SITE_API_URL=https://your-backend.example.com   # required
SITE_X_HANDLE=YourBot                           # optional, no @
SITE_NAME=VIGIL                                 # optional
```

Then `vercel link` once and `scripts/deploy-web.sh`. On another static host, edit `web/config.js` and those
`<head>` tags by hand, then upload `web/`.

### Make it yours

If you run a public copy, give it its own name, X account and site so nobody mistakes it for the original:
`CT_AGENT_NAME`, `CT_AGENT_TICKER`, `CT_X_HANDLE`, `SITE_NAME`, `SITE_X_HANDLE`, and the art in `web/assets/`
and `brand/` (`scripts/make_brand.py` redraws it).

## API

Reads are public; `/admin/*` needs `Authorization: Bearer $CT_API_ADMIN_TOKEN` and is disabled while that
token is unset. Browsers can't send that header cross-site (CORS rejects it), so admin calls stay server-side.

`GET /health` `/candidates` `/tokens` `/tokens/{mint}` `/decisions` `/positions` `/scorecard` `/journal`
`/journal/verify` `/sources` `/leads` `/posts` `/scanner` `/outcomes` `/site` `/launchpad/status` `/launchpad/recent`
`/logo/{mint}` `/chat/limits`
`POST /chat` `/chat/stream` (rate limited, off by default)
`POST /admin/kill` `/admin/sources` `/admin/transcripts` `/admin/posts` `/admin/posts/{id}/resolve`
`POST /webhooks/twitch` (signature-verified EventSub)

Interactive docs at `/docs` while the API runs.

## Solana facts the code relies on

- Amounts: SOL has 9 decimals (lamports); each coin has its own (pump.fun coins use 6), read from the mint.
- Swaps go through Jupiter's Swap V2 `order` → sign → `execute`, with RFQ routes excluded so the bot's signature
  is the only one needed. Exit marks come from Jupiter's Price API (one batched call per check); a real quote is
  only taken when something actually sells.
- Both token programs are supported: SPL Token and Token-2022 (whose extensions are checked).
- A coin is `curve` while it's on a launchpad bonding curve (e.g. pump.fun) and `pool` once it trades in a pool
  (e.g. PumpSwap, Raydium), from its deepest SOL pair on DexScreener.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md). To report a vulnerability, follow [SECURITY.md](SECURITY.md) and
please don't open a public issue.

Released under the [MIT License](LICENSE). VIGIL is an independent project, not affiliated with xAI, X, Solana, Jupiter or pump.fun.
