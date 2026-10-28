PRAGMA foreign_keys = ON;

-- Append-only journal. Every fill, exit, partial fill, failed tx, decision,
-- post and correction lands here. Each row carries the hash of the previous
-- row so any later edit breaks the chain and is detectable.
CREATE TABLE IF NOT EXISTS journal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    ref         TEXT,
    mode        TEXT,
    payload     TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_journal_kind ON journal(kind, id);
CREATE INDEX IF NOT EXISTS ix_journal_ref ON journal(ref, id);

CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Every Solana coin VIGIL has looked at (from scanner calls, chat look-ups, launches), filled on demand.
CREATE TABLE IF NOT EXISTS tokens (
    mint            TEXT PRIMARY KEY,        -- base58, case-sensitive: never lowercased
    name            TEXT,
    symbol          TEXT,
    decimals        INTEGER NOT NULL,
    program         TEXT,                    -- SPL Token or Token-2022
    stage           TEXT,                    -- 'curve' (still on a launchpad bonding curve) | 'pool' (graduated)
    dex_id          TEXT,                    -- the deepest SOL pool's DEX (pumpfun, pumpswap, raydium, ...)
    pool_id         TEXT,                    -- that pool's address
    logo_url        TEXT,                    -- '' if it has none
    creator         TEXT,
    first_seen      TEXT NOT NULL,
    last_checked    TEXT
);

-- DEX aggregator snapshots. Indicative only, never a live quote.
CREATE TABLE IF NOT EXISTS market_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL,
    ts              TEXT NOT NULL,
    source          TEXT NOT NULL,
    pool_address    TEXT,
    price_usd       REAL,
    volume_h1_usd   REAL,
    volume_h24_usd  REAL,
    liquidity_usd   REAL,
    txns_h1         INTEGER,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS ix_market_token ON market_snapshots(token, id);

-- Attributed evidence: news, X posts, Twitch transcripts/chat, manual notes.
CREATE TABLE IF NOT EXISTS sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK (kind IN ('news','x','twitch_chat','twitch_transcript','manual')),
    origin_url      TEXT,
    origin_id       TEXT,
    author          TEXT,
    channel         TEXT,
    published_at    TEXT,
    captured_at     TEXT NOT NULL,
    excerpt         TEXT NOT NULL,
    confidence      TEXT NOT NULL CHECK (confidence IN ('verified','first_party','reported','rumor','sentiment')),
    claim_key       TEXT,
    cites_url       TEXT,
    raw             TEXT,
    UNIQUE (kind, origin_id)
);
CREATE INDEX IF NOT EXISTS ix_sources_claim ON sources(claim_key);

-- Contract addresses found in speech/chat/posts. Leads only until verified.
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    address         TEXT NOT NULL,
    chain_hint      TEXT,
    status          TEXT NOT NULL DEFAULT 'unverified'
                    CHECK (status IN ('unverified','verified','rejected')),
    verified_token  TEXT,
    verify_note     TEXT,
    created_at      TEXT NOT NULL,
    verified_at     TEXT,
    UNIQUE (source_id, address)
);

CREATE TABLE IF NOT EXISTS decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    token         TEXT NOT NULL,
    stage         TEXT NOT NULL,
    action        TEXT NOT NULL CHECK (action IN ('buy','pass','watch','sell','hold')),
    confidence    REAL,
    thesis        TEXT,
    reasons       TEXT,
    risks         TEXT,
    source_ids    TEXT,
    model         TEXT,
    raw           TEXT
);
CREATE INDEX IF NOT EXISTS ix_decisions_token ON decisions(token, id);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('paper','live')),
    status          TEXT NOT NULL CHECK (status IN ('open','closed')),
    decision_id     INTEGER REFERENCES decisions(id),
    quote_symbol    TEXT NOT NULL,
    opened_at       TEXT NOT NULL,
    entry_quote     REAL NOT NULL,
    entry_tokens    REAL NOT NULL,
    entry_price     REAL NOT NULL,
    closed_at       TEXT,
    exit_quote      REAL,
    exit_price      REAL,
    pnl_quote       REAL,
    exit_reason     TEXT,
    tokens_left     REAL,               -- NULL until the first partial sell; then what is still held
    realized_quote  REAL NOT NULL DEFAULT 0,  -- SOL already taken off by partial sells
    tp_hits         INTEGER NOT NULL DEFAULT 0 -- take-profit rungs already sold
);
CREATE INDEX IF NOT EXISTS ix_positions_status ON positions(status, mode);

-- X outbox. idempotency_key prevents double-posting on retries.
CREATE TABLE IF NOT EXISTS posts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key  TEXT NOT NULL UNIQUE,
    kind             TEXT NOT NULL,
    body             TEXT NOT NULL,
    ref              TEXT,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','sending','sent','failed','cancelled','unknown')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    x_post_id        TEXT,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    sent_at          TEXT,
    reply_to_key     TEXT,           -- idempotency_key of the post this one replies to (threads)
    media            TEXT            -- JSON list of image files in ct_agent/social/media to attach
);

-- What happened after every call (buy/watch/pass): variables at the time and
-- spot price at fixed checkpoints, for tuning the filters.
CREATE TABLE IF NOT EXISTS call_outcomes (
    decision_id  INTEGER PRIMARY KEY REFERENCES decisions(id),
    token        TEXT NOT NULL,
    symbol       TEXT,
    stage        TEXT NOT NULL,
    action       TEXT NOT NULL,
    decided_at   TEXT NOT NULL,
    venue0       TEXT,
    price0       REAL,
    features     TEXT NOT NULL,
    p15m REAL, p1h REAL, p4h REAL, p24h REAL,
    peak REAL, trough REAL,
    graduated    INTEGER NOT NULL DEFAULT 0,
    done         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_call_outcomes_open ON call_outcomes(done, decided_at);
CREATE INDEX IF NOT EXISTS ix_call_outcomes_token ON call_outcomes(token, decided_at);

-- Calls from the smart-money scanner (Supabase alert_performance), and how far each has moved since.
CREATE TABLE IF NOT EXISTS scanner_calls (
    id              INTEGER PRIMARY KEY,
    token           TEXT NOT NULL,
    symbol          TEXT,
    called_at       TEXT NOT NULL,
    call_price_usd  REAL,
    call_mcap_usd   REAL,
    data            TEXT NOT NULL,
    status          TEXT NOT NULL,          -- watching | bought | expired | skipped
    note            TEXT,
    pool_id         TEXT,
    last_price_usd  REAL,
    last_move_pct   REAL,
    peak_move_pct   REAL,
    triggered_at    TEXT,
    checked_at      TEXT,
    first_seen      TEXT NOT NULL,
    decision_id     INTEGER                 -- Grok's analysis of the call
);
CREATE INDEX IF NOT EXISTS ix_scanner_calls_status ON scanner_calls(status, called_at);
CREATE INDEX IF NOT EXISTS ix_scanner_calls_token ON scanner_calls(token);

-- Public "Ask VIGIL" chat: one row per request, for rate limits. `who` is a salted hash of the IP.
CREATE TABLE IF NOT EXISTS chat_requests (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    who     TEXT NOT NULL,
    token   TEXT,
    status  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chat_requests_who ON chat_requests(who, ts);
CREATE INDEX IF NOT EXISTS ix_chat_requests_ts ON chat_requests(ts);

-- Launchpad: coins VIGIL launches on pump.fun for people who ask on X. One row per request, from the tweet to
-- the coin's creator-fee split (90% to the requester, 10% to VIGIL, set once at launch and locked by pump.fun).
CREATE TABLE IF NOT EXISTS launch_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    x_post_id       TEXT NOT NULL UNIQUE,   -- the request tweet (VIGIL replies under it)
    author_id       TEXT NOT NULL,
    handle          TEXT,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('refused','approved','launching','launched','failed')),
    reason          TEXT,                   -- why it was refused or failed
    name            TEXT,
    symbol          TEXT,
    description     TEXT,
    logo            TEXT,
    socials         TEXT,                   -- JSON {twitter, telegram, website}
    recipient       TEXT,                   -- the requester's Solana wallet: gets their share of the creator fees
    token           TEXT,                   -- the new coin's mint
    metadata_uri    TEXT,                   -- IPFS metadata the coin points to
    tx_hash         TEXT,                   -- the create transaction signature
    cost_lamports   TEXT,                   -- what the launch cost VIGIL (fees, rent, dev buy)
    fee_share_tx    TEXT,                   -- the transaction that set the creator-fee split
    fee_shares      TEXT,                   -- JSON [{address, bps}] as set on chain
    launched_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_launch_requests_author ON launch_requests(author_id, created_at);
CREATE INDEX IF NOT EXISTS ix_launch_requests_status ON launch_requests(status);
