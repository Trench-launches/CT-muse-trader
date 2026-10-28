"""Launch requests from X, the pump.fun launches themselves, and each coin's creator-fee split.

1. take_request (in the mentions loop): a post asking VIGIL to launch a coin is checked (per-account daily limit,
   account age, one Solana wallet for the fees, an attached image, the daily SOL budget) and screened by Grok (a real
   request, no impersonation, nothing unsafe). It's approved, or refused with a short reply.
2. launchpad_once (its own loop): approved requests are announced on X, then launched on pump.fun from the launcher
   wallet, and VIGIL replies with the contract address. Right after, the coin's creator fees are split once and
   pump.fun locks it: launch_creator_share_bps (90%) to the requester's wallet, the rest to the launcher. Coins with a
   split get their fees paid out every few hours.

A request with a tx_hash is never launched again: if the outcome of a send is unknown, the loop asks the chain.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from solders.pubkey import Pubkey

from ..chain import keys
from ..db import journal_append, kv_get, kv_set, rows, utcnow
from ..social.compose import Draft
from ..social.outbox import enqueue
from ..units import lamports, sol
from .pumpfun import FeeSplitUnverified, LaunchBlocked, LaunchUnconfirmed, PumpLauncher, split_for

log = logging.getLogger(__name__)
ASK = re.compile(r"\b(launch|deploy)\b", re.I)
SYMBOL = re.compile(r"^[A-Za-z0-9]{1,10}$")
EST_OVERHEAD_LAMPORTS = lamports(0.03)  # a launch's rent + fees + the split, on top of the dev buy, before it happens
SOCIALS = ("twitter", "telegram", "website")  # what pump.fun's metadata shows
UNSENT_FAILS_AFTER_S = 180   # a signed launch the chain still doesn't know after this can't land (its blockhash expired)
RETRY_SPLIT_S = 600          # a split that couldn't go out is tried again after this
DISTRIBUTE_EVERY_S = 6 * 3600
# Addresses in a post that are never the requester's wallet
NOT_A_WALLET = {keys.SOL_MINT, keys.USDC_MINT, keys.SYSTEM_PROGRAM, keys.TOKEN_PROGRAM, keys.PUMP_PROGRAM,
                keys.PUMP_AMM_PROGRAM, keys.INCINERATOR, "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
                "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ", "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"}


def looks_like_request(text: str) -> bool:
    return bool(ASK.search(text or ""))


def _since(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _est_cost(s: Any) -> int:
    return lamports(max(0.0, float(s.launch_dev_buy_sol or 0))) + EST_OVERHEAD_LAMPORTS


def _spent_24h(conn: Any, s: Any) -> int:
    """Lamports spent on launches in the last 24h, counting approved ones still waiting at the estimate."""
    done = sum(int(r["cost_lamports"] or 0) for r in rows(
        conn, "SELECT cost_lamports FROM launch_requests WHERE launched_at>=?", (_since(24),)))
    waiting = conn.execute("SELECT COUNT(*) c FROM launch_requests WHERE status IN ('approved','launching')").fetchone()["c"]
    return done + waiting * _est_cost(s)


def _wallets(text: str, s: Any) -> list[str]:
    """Solana wallets in the post, as written (base58 is case-sensitive). Program ids, well-known mints, VIGIL's own
    coin and anything off the ed25519 curve (token accounts, PDAs: they can't be someone's wallet) are skipped."""
    skip = NOT_A_WALLET | {a for a in (getattr(s, "agent_token_address", ""),) if a}
    out = []
    for a in keys.find_pubkeys(text):
        if a in skip:
            continue
        if not Pubkey.from_string(a).is_on_curve():
            continue
        out.append(a)
    return out


def _reply(conn: Any, m: dict[str, Any], text: str, kind: str = "launch") -> None:
    enqueue(conn, Draft(kind, text, f"x_user:{m.get('author_id')}", f"launch:{m['id']}:{kind}", reply_to=f"x:{m['id']}"))


def _refuse(conn: Any, m: dict[str, Any], reason: str, reply: str, **fields: Any) -> str:
    author = m.get("author") or {}
    conn.execute("INSERT OR IGNORE INTO launch_requests(x_post_id,author_id,handle,created_at,status,reason,name,symbol) "
                 "VALUES(?,?,?,?,?,?,?,?)", (m["id"], m.get("author_id") or "", author.get("username"), utcnow(),
                                             "refused", reason, fields.get("name"), fields.get("symbol")))
    journal_append(conn, "launchpad_refused", {"x_post_id": m["id"], "reason": reason, **fields})
    _reply(conn, m, reply)
    return "refused"


def take_request(ctx: Any, m: dict[str, Any]) -> str:
    """Handle one mention that may be a launch request. Returns 'not_request' (answer it as a normal reply),
    'seen', 'refused' or 'approved'."""
    s, conn = ctx.settings, ctx.conn
    if conn.execute("SELECT 1 FROM launch_requests WHERE x_post_id=?", (m["id"],)).fetchone():
        return "seen"
    text, author = m.get("text", ""), m.get("author") or {}
    res = ctx.brain.review_launch(text)
    d = res.decision
    if d is None:
        log.warning("launch review unavailable for %s: %s", m["id"], res.error)
        return "not_request"  # can't read it now: a normal reply, and it can be asked again
    if not d.is_request:
        return "not_request"
    sym = d.symbol.strip().lstrip("$").upper()
    name = " ".join(d.name.split())[:32]
    fields = {"name": name, "symbol": sym}
    # the house rules first, the same for everyone
    n = conn.execute("SELECT COUNT(*) c FROM launch_requests WHERE author_id=? AND status!='refused' AND created_at>=?",
                     (m.get("author_id") or "", _since(24))).fetchone()["c"]
    vip = (author.get("username") or "").lower() in {h.lower().lstrip("@") for h in s.launch_vip_handles}
    if n >= (s.launch_vip_per_day if vip else s.launch_per_user_day):
        return _refuse(conn, m, "per-user limit", "One launch per account per day, fren. Come back tomorrow 🌅", **fields)
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(str(author.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        age = timedelta(0)
    if age < timedelta(days=s.launch_min_account_days):
        return _refuse(conn, m, "account too new", f"Launches are open to X accounts older than "
                                                  f"{s.launch_min_account_days} days, ser. Try again later.", **fields)
    wallets = _wallets(text, s)
    if len(wallets) != 1:
        return _refuse(conn, m, "no wallet", "Add the Solana wallet that should get the creator fees (one address) "
                                              "and ask again 🌅", **fields)
    if not m.get("images"):
        return _refuse(conn, m, "no image", "Attach an image for the coin and ask again 🌅", **fields)
    if not d.approve:
        return _refuse(conn, m, "grok declined: " + d.reason, (d.reason or "I can't launch that one.")[:200], **fields)
    if not name or not SYMBOL.match(sym) or sym.lower() not in text.lower() or name.lower() not in text.lower():
        return _refuse(conn, m, "unclear name or ticker", "Give me a name and a ticker (letters and numbers, up to 10) "
                                                          "and I'll launch it 🌅", **fields)
    own = (s.agent_ticker or "VIGIL").upper()  # an empty CT_AGENT_TICKER still reserves the default
    if sym == own:  # any name goes, except a copy of VIGIL's own coin
        return _refuse(conn, m, "reserved ticker", f"${own} is my own coin, so I can't launch another one. "
                                                   "Pick another ticker 🌅", **fields)
    if _spent_24h(conn, s) + _est_cost(s) > lamports(s.launch_daily_cap_sol):
        return _refuse(conn, m, "daily budget used", "Launches are full for today. Ask again tomorrow 🌅", **fields)
    socials = {k: getattr(d, k, "") for k in SOCIALS if getattr(d, k, "")}
    conn.execute("INSERT INTO launch_requests(x_post_id,author_id,handle,created_at,status,name,symbol,description,logo,"
                 "socials,recipient) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (m["id"], m.get("author_id") or "", author.get("username"), utcnow(), "approved", name, sym,
                  d.description[:280], m["images"][0], json.dumps(socials), wallets[0]))
    journal_append(conn, "launchpad_approved", {"x_post_id": m["id"], "handle": author.get("username"),
                                                "recipient": wallets[0], **fields})
    return "approved"


# --- the loop ----------------------------------------------------------------------------------------------------------

def launchpad_once(ctx: Any, launcher: PumpLauncher | None = None) -> dict[str, Any]:
    s, conn = ctx.settings, ctx.conn
    if not s.launchpad_enabled:
        return {"launchpad": "off"}
    launcher = launcher or PumpLauncher(s, ctx.rpc, conn, ctx.web)
    launched = split = paid = 0
    for r in rows(conn, "SELECT * FROM launch_requests WHERE status='launching' ORDER BY id"):
        try:
            launched += _settle(ctx, launcher, r)
        except Exception as exc:  # one stuck launch never stops the others; asked again next loop
            log.warning("checking launch %s failed: %s", r["id"], exc)
    for r in rows(conn, "SELECT * FROM launch_requests WHERE status='approved' ORDER BY id LIMIT 3"):
        launched += _launch(ctx, launcher, r)
    for r in rows(conn, "SELECT * FROM launch_requests WHERE status='launched' AND fee_shares IS NULL ORDER BY id"):
        split += _split(ctx, launcher, r)
    for r in rows(conn, "SELECT * FROM launch_requests WHERE status='launched' AND fee_shares IS NOT NULL ORDER BY id"):
        paid += _distribute(ctx, launcher, r)
    return {"launched": launched, "fee_splits": split, "distributed": paid}


def announce_key(r: dict[str, Any]) -> str:
    return f"launch_announce:{r['id']}"


def _by(r: dict[str, Any]) -> str:
    return f"@{r['handle']}" if r.get("handle") else "its creator"


def _pct(bps: int) -> str:
    return f"{bps / 100:g}%"


def _launch(ctx: Any, launcher: PumpLauncher, r: dict[str, Any]) -> int:
    """Announce, launch, then post the receipt. The coin's X link is written into its metadata at launch and can't
    change, so VIGIL's announcement goes out first and the coin links to it (unless they gave their own link)."""
    conn, s = ctx.conn, ctx.settings
    if r.get("tx_hash"):  # signed before: never again
        conn.execute("UPDATE launch_requests SET status='launching' WHERE id=?", (r["id"],))
        return 0
    key = announce_key(r)
    ann = conn.execute("SELECT status, x_post_id FROM posts WHERE idempotency_key=?", (key,)).fetchone()
    if ann is None:
        desc = f"\n\n{r['description']}" if r["description"] else ""
        creator = f"\nCreator: @{r['handle']}" if r["handle"] else ""
        enqueue(conn, Draft("launch_announce", f"🚀 New on the VIGIL launchpad\n\n${r['symbol']} · {r['name']}{creator}"
                                               f"{desc}\n\nLaunching now on pump.fun. CA and receipt below 👇",
                            f"launch:{r['id']}", key))
        return 0
    if ann["status"] in ("pending", "sending"):
        return 0  # the coin links to that post: launch once it's out
    posted = ann["status"] == "sent" and ann["x_post_id"]
    if conn.execute("UPDATE launch_requests SET status='launching' WHERE id=? AND status='approved'",
                    (r["id"],)).rowcount != 1:
        return 0
    socials = json.loads(r["socials"] or "{}")
    if not socials.get("twitter") and posted:
        socials["twitter"] = f"https://x.com/i/status/{ann['x_post_id']}"  # works whatever the handle becomes
    via = f"@{s.x_handle}" if s.x_handle else "VIGIL"
    credit = f"Created by @{r['handle']}, launched via {via}" if r["handle"] else f"Launched via {via}"
    own = (r["description"] or "").strip().rstrip(".").strip()
    desc = f"{own}. {credit}" if own else credit  # one full stop, whatever they typed

    def signed(mint: str, sig: str) -> None:  # recorded before it goes out: this request is never launched twice
        conn.execute("UPDATE launch_requests SET token=?, tx_hash=? WHERE id=?", (mint, sig, r["id"]))
        kv_set(conn, f"launch_signed_at:{r['id']}", time.time())

    try:
        uri = r.get("metadata_uri")
        if not uri:
            uri = launcher.upload_metadata(r["name"], r["symbol"], desc, r["logo"], socials)
            conn.execute("UPDATE launch_requests SET metadata_uri=?, socials=? WHERE id=?",
                         (uri, json.dumps(socials), r["id"]))
        out = launcher.launch(r["name"], r["symbol"], uri, s.launch_dev_buy_sol, on_signed=signed)
    except LaunchUnconfirmed as exc:
        log.warning("launch %s sent but not confirmed yet: %s", r["id"], exc)
        journal_append(conn, "launchpad_unconfirmed", {"id": r["id"], "signature": exc.signature, "mint": exc.mint,
                                                       "error": str(exc)[:300]}, ref=exc.mint, mode="live")
        return 0  # stays 'launching' with its tx_hash: the next loop asks the chain
    except LaunchBlocked as exc:
        _fail(ctx, r, str(exc), posted)
        return 0
    except Exception as exc:  # an RPC that stayed down through its retries, say
        if conn.execute("SELECT tx_hash FROM launch_requests WHERE id=?", (r["id"],)).fetchone()["tx_hash"]:
            log.warning("launch %s signed, then: %s", r["id"], exc)  # stays 'launching': the next loop asks the chain
            return 0
        _fail(ctx, r, f"launch error: {type(exc).__name__}: {exc}", posted)
        return 0
    _launched(ctx, launcher, r, out.mint, out.signature, out.cost_lamports, posted, ann["x_post_id"] if posted else None)
    return 1


def _fail(ctx: Any, r: dict[str, Any], why: str, posted: Any) -> None:
    conn = ctx.conn
    conn.execute("UPDATE launch_requests SET status='failed', reason=? WHERE id=?", (why[:300], r["id"]))
    journal_append(conn, "launchpad_launch_failed", {"id": r["id"], "symbol": r["symbol"], "reason": why[:300]})
    if posted:
        sorry = f"❌ ${r['symbol']} didn't launch\n\nSomething went wrong on my side and no coin was made. Sorry fren."
        enqueue(conn, Draft("launch_receipt", sorry, f"launch:{r['id']}", f"launch_receipt:{r['id']}",
                            reply_to=announce_key(r)))
    _reply(conn, {"id": r["x_post_id"], "author_id": r["author_id"]},
           "Couldn't launch that one right now, something went wrong on my side. Sorry fren, try again later 🌅",
           kind="launch_failed")


def _launched(ctx: Any, launcher: PumpLauncher, r: dict[str, Any], mint: str, sig: str, cost: int | None,
              posted: Any, ann_x_id: str | None) -> None:
    conn, s = ctx.conn, ctx.settings
    conn.execute("UPDATE launch_requests SET status='launched', token=?, tx_hash=?, cost_lamports=?, launched_at=? "
                 "WHERE id=?", (mint, sig, str(cost) if cost is not None else None, utcnow(), r["id"]))
    shares = split_for(r["recipient"], launcher.wallet or "", s.launch_creator_share_bps)
    theirs = next((sh.bps for sh in shares if sh.address == r["recipient"]), 0)
    explorer = (s.explorer_url or "https://solscan.io").rstrip("/")
    pump = f"https://pump.fun/coin/{mint}"
    fees = (f"{_pct(theirs)} to {_by(r)}, {_pct(10_000 - theirs)} to VIGIL" if 0 < theirs < 10_000
            else f"all to {_by(r)}" if theirs else "all to VIGIL")
    cost_line = f"Launch cost: {sol(cost):.4f} SOL (paid by VIGIL)\n" if cost is not None else ""
    receipt = (f"✅ ${r['symbol']} is live on Solana\n\nCA: {mint}\nTx: {explorer}/tx/{sig}\n{pump}\n\n"
               f"{cost_line}Creator fees: {fees} (set on pump.fun right after launch, then locked)")
    if posted:
        enqueue(conn, Draft("launch_receipt", receipt, mint, f"launch_receipt:{r['id']}", reply_to=announce_key(r)))
    link = f"https://x.com/i/status/{ann_x_id}" if ann_x_id else pump
    yours = (f"{_pct(theirs)} of its creator fees go to your wallet. The split is set on pump.fun right after "
             "launch and then locked." if theirs else "")
    _reply(conn, {"id": r["x_post_id"], "author_id": r["author_id"]},
           f"🚀 ${r['symbol']} is live on Solana\n\nCA: {mint}\n\n{yours}\n\n{link}".replace("\n\n\n\n", "\n\n"))
    # they asked before and it was refused (no image, say): the answer goes under that first post too
    for old in rows(conn, "SELECT x_post_id FROM launch_requests WHERE author_id=? AND upper(symbol)=? AND "
                          "status='refused' AND id<?", (r["author_id"], r["symbol"].upper(), r["id"])):
        _reply(conn, {"id": old["x_post_id"], "author_id": r["author_id"]},
               f"🚀 ${r['symbol']} is live now\n\nCA: {mint}\n\nReceipt: {link}", kind="launch_followup")
    r = {**r, "status": "launched", "token": mint, "tx_hash": sig}
    _split(ctx, launcher, r)  # straight away: the split is cheapest while the curve has barely traded


def _settle(ctx: Any, launcher: PumpLauncher, r: dict[str, Any]) -> int:
    """A launch left 'launching' (sent but unconfirmed, or the process stopped mid-launch): ask the chain."""
    conn = ctx.conn
    if not r.get("tx_hash"):  # never signed, so nothing went out: it can simply be tried again
        conn.execute("UPDATE launch_requests SET status='approved' WHERE id=? AND tx_hash IS NULL", (r["id"],))
        return 0
    ann = conn.execute("SELECT status, x_post_id FROM posts WHERE idempotency_key=?", (announce_key(r),)).fetchone()
    posted = ann and ann["status"] == "sent" and ann["x_post_id"]
    st = ctx.rpc.signature_statuses([r["tx_hash"]])[0]
    if st and not st.get("err") and st.get("confirmationStatus") in ("confirmed", "finalized"):
        _launched(ctx, launcher, r, r["token"], r["tx_hash"], launcher.tx_cost(r["tx_hash"]), posted,
                  ann["x_post_id"] if posted else None)
        return 1
    if st and st.get("err"):
        _fail(ctx, r, f"launch transaction failed on chain: {st['err']}", posted)
        return 0
    signed_at = kv_get(conn, f"launch_signed_at:{r['id']}", 0) or 0
    if st is None and time.time() - float(signed_at) > UNSENT_FAILS_AFTER_S:
        _fail(ctx, r, "launch transaction never landed", posted)  # its blockhash has expired: it can't land now
    return 0


def _due(conn: Any, key: str) -> bool:
    return time.time() >= float(kv_get(conn, key, 0) or 0)


def _split(ctx: Any, launcher: PumpLauncher, r: dict[str, Any]) -> int:
    """Set the coin's creator-fee split once (requester first, then VIGIL). Retried until it's on chain."""
    conn, s = ctx.conn, ctx.settings
    key = f"launch_split_next:{r['id']}"
    if not _due(conn, key):
        return 0
    shares = split_for(r["recipient"], launcher.wallet or "", s.launch_creator_share_bps)
    try:
        sig = launcher.set_fee_split(r["token"], shares)
    except FeeSplitUnverified as exc:
        kv_set(conn, key, time.time() + RETRY_SPLIT_S)
        if not kv_get(conn, f"launch_split_unverified:{r['id']}"):  # journal it once, with the simulation, for review
            kv_set(conn, f"launch_split_unverified:{r['id']}", True)
            journal_append(conn, "launchpad_fee_split_unverified",
                           {"id": r["id"], "mint": r["token"], "shares": [x.as_dict() for x in shares],
                            "reason": str(exc)[:300], "simulation": exc.simulation}, ref=r["token"], mode="live")
        log.warning("fee split for %s not sent: %s", r["token"], exc)
        return 0
    except Exception as exc:  # LaunchBlocked or an RPC hiccup: try again later
        kv_set(conn, key, time.time() + RETRY_SPLIT_S)
        log.warning("fee split for %s failed: %s", r["token"], exc)
        journal_append(conn, "launchpad_fee_split_failed", {"id": r["id"], "mint": r["token"],
                                                            "error": str(exc)[:300]}, ref=r["token"], mode="live")
        return 0
    conn.execute("UPDATE launch_requests SET fee_share_tx=?, fee_shares=? WHERE id=?",
                 (sig, json.dumps([x.as_dict() for x in shares]), r["id"]))
    theirs = next((x.bps for x in shares if x.address == r["recipient"]), 0)
    if sig and theirs:
        explorer = (s.explorer_url or "https://solscan.io").rstrip("/")
        _reply(conn, {"id": r["x_post_id"], "author_id": r["author_id"]},
               f"🔒 ${r['symbol']} creator fees are split on chain and locked: {_pct(theirs)} to your wallet.\n\n"
               f"{explorer}/tx/{sig}", kind="launch_split")
    return 1


def _distribute(ctx: Any, launcher: PumpLauncher, r: dict[str, Any]) -> int:
    conn = ctx.conn
    key = f"launch_distribute_next:{r['id']}"
    if not _due(conn, key):
        return 0
    kv_set(conn, key, time.time() + DISTRIBUTE_EVERY_S)
    try:
        return 1 if launcher.distribute_fees(r["token"]) else 0
    except Exception as exc:  # retried on the next round
        log.warning("fee payout for %s failed: %s", r["token"], exc)
        return 0
