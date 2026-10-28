# VIGIL: an open, automated memecoin trader for Robinhood Chain

VIGIL is an AI trading persona that runs in the open. It watches new
Pons V2 memecoin launches on Robinhood Chain (chain id 4663), checks each
one onchain, asks xAI's **Grok** whether it's worth a trade, applies hard risk limits Grok can't override,
journals every decision in a tamper-evident log, and posts its trades (wins and losses) on X.
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
 Robinhood Chain RPC ──► indexer ──┐                       ┌──► social ──► X API (posts, replies)
 (launches, phases, curve trades)  │                       │      outbox: daily cap, idempotent,
                                   ▼                       │      never blind-retries an ambiguous send
 news RSS / X reads / Twitch ──► research ──► SQLite ◄──── trader / exits / caller
 (budgeted, attributed)   verify leads     (append-only    screen → Grok → hard gates → fill
                          onchain          hash-chained    → exits (stop / take-profit / max-hold)
                                           journal)
                                              ▲
                                              └── api (public reads, admin writes) ◄── web/ (static site)
```

Each box is a separate OS process, started together by `ctagent run-all` (or one container each with
docker compose), so an X outage can't stop trading and a crash in one worker can't take down the rest.

| Worker | What it does |
| --- | --- |
| `indexer` | Reads Pons launches and curve trades from the chain into SQLite |
| `research` | Pulls news, X posts and Twitch chat; links mentions to coins only after verifying them onchain |
| `caller` / `trader` | Screens candidates, asks Grok, applies the gates, opens (paper or live) positions |
| `exits` | Checks stop-loss, take-profit ladder and max-hold every few seconds |
| `social` | Writes and sends posts and replies through a rate-limited outbox |
| `launchpad` | Optional: launches coins for people who ask on X (off by default) |
| `api` | FastAPI: public read endpoints for the site, token-protected admin endpoints |

### Rules enforced in code

| Rule | Where |
| --- | --- |
| Only Pons V2 launches, verified onchain via the factory record | `chain/pons.py` `verify_token` |
| Genuine V4 pool only; lookalike pools rejected by computed PoolId | `PonsV2.genuine_pool_id`, `market/dex.py` |
| Never buy inside the launch snipe-tax window (read live from the factory) | `strategy/screen.py` |
| Deployer concentration, holder concentration, thin-chart and wash-trade guards | `strategy/engine.py` |
| Addresses from chat, speech or posts are leads until verified on this chain | `sources/store.py` |
| Third-party text is fenced as untrusted in the Grok prompt | `brain/grok.py` |
| Grok cannot override gates, limits or the kill switch | `strategy/engine.py` |
| Paper first; paper trades labelled and never shown as onchain | `execution/paper.py`, `social/compose.py` |
| Live needs mode=live, a human review flag, a trading-only key, a quote, a slippage bound, a tax check and a simulation | `execution/live.py` |
| Position, exposure, daily-loss and daily-entry limits, and a kill switch | `execution/risk.py` |
| Dangerous settings refused at start-up (e.g. live with no admin token) | `safety.py` |
| Every fill, exit and failure journalled, append-only and hash-chained | `db.py`, `schema.sql` |

## Quick start (local, paper mode)

You need Python 3.12+. Nothing else is required to start: with no keys the bot indexes the chain read-only.

```bash
git clone https://github.com/Trench-launches/CT-muse-trader.git vigil && cd vigil
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows: .venv\Scripts\pip install -e ".[dev]"
cp .env.example .env                     # then fill in what you have (see "Keys" below)
.venv/bin/ctagent check                  # tests each connection and prints a safety review
.venv/bin/ctagent run indexer --once     # one indexing pass
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
| Robinhood Chain RPC | `CT_RPC_URL` | The public RPC works for testing. For production use a provider such as [Alchemy](https://dashboard.alchemy.com) (Robinhood Chain mainnet); the URL then contains your key, so treat it as a secret. |
| Smart-money scanner (optional) | `CT_SUPABASE_URL`, `CT_SUPABASE_KEY` | Only if you run your own scanner that writes calls to Supabase. Otherwise set `CT_CANDIDATE_SOURCE=pons`. |
| Twitch (optional) | `CT_TWITCH_*` | [dev.twitch.tv/console](https://dev.twitch.tv/console). |
| Trading wallet (live only) | `CT_TRADING_WALLET_PRIVATE_KEY` | Make a **new** wallet just for the bot and fund it with a small amount. See [SECURITY.md](SECURITY.md). |
| Admin token | `CT_API_ADMIN_TOKEN` | Make one: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |

### Going live (real money)

Only after you've run on paper long enough to trust the results, and read `ct_agent/execution/live.py`:

1. Create a new wallet for the bot only, and fund it with what you can afford to lose.
2. Set `CT_TRADING_WALLET_PRIVATE_KEY`, a 32+ character `CT_API_ADMIN_TOKEN`, `CT_LIVE_EXECUTION_REVIEWED=true`
   and finally `CT_TRADING_MODE=live`.
3. Run `ctagent check` (it should say `live_trading: READY` and `safety: ok`), then
   `ctagent live-test <coin> --eth 0.002` for one tiny round trip before letting the bot run.
4. Keep the limits small (`CT_MAX_POSITION_ETH`, `CT_MAX_TOTAL_EXPOSURE_ETH`, `CT_MAX_DAILY_LOSS_ETH`).

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

The site reads its settings from `web/config.js`. `scripts/deploy-web.sh` writes that file from `SITE_*`
variables in a temporary copy, so your values never need committing. Put them in a gitignored `.env.site`:

```bash
SITE_API_URL=https://your-backend.example.com   # required
SITE_X_HANDLE=YourBot                           # optional, no @
SITE_NAME=VIGIL                                 # optional
```

Then `vercel link` once and `scripts/deploy-web.sh`. On another static host, edit `web/config.js` and upload
`web/`. Also update the `<head>` tags in `web/index.html` (canonical URL, `og:*`, `twitter:site`) to your
domain and X account.

### Make it yours

If you run a public copy, give it its own name, X account and site so nobody mistakes it for the original:
`CT_AGENT_NAME`, `CT_AGENT_TICKER`, `CT_X_HANDLE`, `SITE_NAME`, `SITE_X_HANDLE`, and the art in `web/assets/`
and `brand/` (`scripts/make_brand.py` redraws it).

## API

Reads are public; `/admin/*` needs `Authorization: Bearer $CT_API_ADMIN_TOKEN` and is disabled while that
token is unset. Browsers can't send that header cross-site (CORS rejects it), so admin calls stay server-side.

`GET /health` `/candidates` `/launches` `/tokens/{address}` `/decisions` `/positions` `/scorecard` `/journal`
`/journal/verify` `/sources` `/leads` `/posts` `/scanner` `/outcomes` `/site` `/launchpad/status` `/chat/limits`
`POST /chat` `/chat/stream` (rate limited, off by default)
`POST /admin/kill` `/admin/sources` `/admin/transcripts` `/admin/posts` `/admin/posts/{id}/resolve`
`POST /webhooks/twitch` (signature-verified EventSub)

Interactive docs at `/docs` while the API runs.

## Chain facts (verified against mainnet, Sep 2026)

- Pons V2 factory `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e`, meme hook `0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044`.
- Graduation at 4.2 ETH on native-quote launches; curve fee 1%; creator tax 0-4% seen.
- Snipe tax 99%, decaying over a few seconds (read live from the factory, never assumed).
- Blocks are ~0.1 s; thousands of launches per day. The public RPC caps `eth_getLogs` at 10,000 logs per query
  and the client shrinks ranges automatically.
- Most graduated tokens have many third-party lookalike V4 pools; only the hooked one is real.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md). To report a vulnerability, follow [SECURITY.md](SECURITY.md) and
please don't open a public issue.

VIGIL is an independent project, not affiliated with xAI, X, Robinhood or Pons.
