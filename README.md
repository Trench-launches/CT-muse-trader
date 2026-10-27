# HORIZON backend

HORIZON is an openly automated AI trading persona on Robinhood Chain (chain 4663),
watching Pons V2 memecoins, reasoning with xAI's Grok (through the xAI API), and
posting its own trades on X. Built to the operating blueprint:
verify before linking, paper first, disclose everything, journal every fill.

This repo holds the backend (`ct_agent/`) and the one-page site (`web/`).

## How it fits together

```
 Robinhood Chain RPC ──► indexer ──┐                       ┌──► social ──► X API (original posts only)
 (launches, phases, curve trades)  │                       │      outbox: 6/day cap, idempotent,
                                   ▼                       │      never blind-retries an ambiguous send
 news RSS / X reads / Twitch ──► research ──► SQLite ◄──── trader
 (budgeted, attributed)   verify leads     (append-only    screen → Grok → hard gates → paper fill
                          onchain          hash-chained    → exits (stop/take-profit/max-hold/Grok review)
                                           journal)
                                              ▲
                                              └── api (public reads, admin writes, Twitch webhook)
```

Each box is a separate OS process (`ctagent run-all` or docker compose).

## Rules enforced in code

| Blueprint rule | Where |
| --- | --- |
| Only Pons V2 launches, verified onchain via the factory record | `chain/pons.py` `verify_token` |
| Skip phase 1 (swept) and 3 (rescued); curve ≥85% or phase 2 pool | `strategy/screen.py` |
| Genuine V4 pool only; lookalike pools rejected by computed PoolId | `PonsV2.genuine_pool_id`, `market/dex.py` |
| Never buy inside the launch snipe-tax window (read live from factory) | `screen_almost_bonded` |
| Acceleration must hold across 5m, 15m and 1h windows | `activity_for` |
| Deployer concentration, one-wallet dominance, round-trip wash clues | `_common_checks` |
| Quote units never mislabelled as USD | `market/money.py`, packs label indicative USD |
| Addresses from chat/speech/posts are leads until verified on this chain | `sources/store.py` `verify_leads` |
| Five reposts of one rumor count as one origin | `independent_origins` |
| Chat is sentiment; rumors alone can never trigger a buy | engine `BUY_GRADE_CONFIDENCE` |
| Third-party text fenced as untrusted in the Grok prompt | `brain/grok.py` |
| Grok cannot override gates, limits or the kill switch | `strategy/engine.py` |
| Paper first; paper trades labelled and never shown as onchain | `execution/paper.py`, `social/compose.py` |
| Live needs mode=live, a human review flag, a trading-only key, quote, slippage bound, tax check, allowance check, simulation | `execution/live.py` |
| Loss, exposure, position, daily-loss limits and a hard stop | `execution/risk.py` |
| Every fill, exit, partial fill and failure journalled, append-only | `db.py`, `schema.sql` triggers + hash chain |
| Losses and corrections posted; holdings and own token disclosed | `social/compose.py` |
| Social separate from trading | separate processes, outbox table |

## Run it

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # Windows; use .venv/bin on Linux/macOS
cp .env.example .env                      # fill in what you have
ctagent check                             # read-only connectivity report
ctagent run indexer --once                # one indexing pass
ctagent run-all                           # every worker + API (http://127.0.0.1:8000/docs)
pytest                                    # 46 tests, offline
```

Or `docker compose up -d --build`.

Stop everything from trading instantly with any of:

```bash
ctagent kill on "reason"
echo stop > data/KILL
curl -X POST localhost:8000/admin/kill -H "authorization: Bearer $CT_API_ADMIN_TOKEN" \
     -H "content-type: application/json" -d '{"on":true,"reason":"manual stop"}'
```

## API (reads are public, `/admin/*` needs the admin token)

`/health` `/candidates` `/launches?stage=almost_bonded|migrated` `/tokens/{address}`
`/decisions` `/positions` `/scorecard` `/journal` `/journal/verify` `/sources` `/leads` `/posts`
`POST /admin/kill` `/admin/sources` `/admin/transcripts` `/admin/posts/{id}/resolve`
`POST /webhooks/twitch` (signature-verified EventSub)

## Facts verified against mainnet on 23 Sep 2026

- Pons V2 factory `0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e`, meme hook
  `0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044`, pool manager `0x8366…0951`.
- Graduation threshold 4.2 ETH on native-quote launches; curve fee 1%; creator tax 0-4% seen.
- Launch fee 0.0005 ETH; `canLaunch` currently open to anyone.
- Snipe tax 99% decaying over **3 s** (the brief's 15 s is the contract default, since lowered).
- Blocks are ~0.1 s; ~3,500 launches and hundreds of thousands of curve trades per 12 h.
- Public RPC caps `eth_getLogs` at 10,000 logs per query; the client auto-shrinks ranges.
- Most graduated tokens have many third-party lookalike V4 pools. Only the hooked one is real.
- ponsfamily.com and its docs are geo-blocked in the UK; contract source is on GitHub.

## Not built yet

- Live execution on graduated V4 pools (needs a reviewed Universal Router adapter). Those
  tokens are paper-only; the live executor refuses them.
- ERC-20-quoted launches are screened but not sized (limits are in ETH).
- Automated VOD transcription: transcripts enter through `POST /admin/transcripts` from an
  authorised pipeline, pending creator rights.
- Dashboard UI and project-token launch (owner name, ticker and wallet needed).
- Postgres for multi-host deployment (SQLite in WAL mode is fine on one host).
