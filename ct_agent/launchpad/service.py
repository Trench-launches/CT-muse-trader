"""Launch requests from X, the launches themselves, and handing each coin's creator fees to its requester.

1. take_request (in the mentions loop): a post asking MOOSE to launch a coin is checked (one per account a day,
   account age, a wallet for the fees, an attached image, the daily budget) and screened by Muse (a real
   request, no impersonation, nothing unsafe). It's approved, or refused with a short reply.
2. launchpad_once (its own loop): approved requests are launched from the launcher wallet, with that wallet as
   the coin's creator-fee recipient, and MOOSE replies with the contract address. Then, for every coin it still
   holds the fees for: once the coin's creator fees cover the launch cost, sweep them and hand the fees to the
   requester for good. A coin that graduates has traded far more than that, so it's handed over too.

A request can pair the coin with a Robinhood stock token instead of ETH ("pair: TSLA"). Its fees are paid in that
stock, which MOOSE has no use for, so a stock-paired coin names the requester as its fee recipient from the start
and MOOSE simply absorbs the (ETH) launch cost.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..chain.abi import ZERO_ADDRESS
from ..chain.pons import Phase
from ..db import journal_append, rows, utcnow
from ..social.compose import Draft
from ..social.outbox import enqueue
from .chain import LaunchBlocked, Launcher
from .pairs import asked_pair, pair_token

log = logging.getLogger(__name__)
ASK = re.compile(r"\b(launch|deploy)\b", re.I)
WALLET = re.compile(r"\b0x[0-9a-fA-F]{40}\b")
SYMBOL = re.compile(r"^[A-Za-z0-9]{1,10}$")
EST_COST_WEI = 700_000_000_000_000  # ~0.0007 ETH: a launch's fee + gas, for the budget before it happens


def looks_like_request(text: str) -> bool:
    return bool(ASK.search(text or ""))


def _since(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _spent_24h(conn: Any) -> int:
    """Wei spent on launches in the last 24h, counting approved ones still waiting at the estimate."""
    done = sum(int(r["cost_wei"] or 0) for r in rows(conn, "SELECT cost_wei FROM launch_requests WHERE launched_at>=?",
                                                        (_since(24),)))
    waiting = conn.execute("SELECT COUNT(*) c FROM launch_requests WHERE status IN ('approved','launching')").fetchone()["c"]
    return done + waiting * EST_COST_WEI


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
        return _refuse(conn, m, "per-user limit", "One launch per account per day, fren. Come back tomorrow 🫎", **fields)
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(str(author.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        age = timedelta(0)
    if age < timedelta(days=s.launch_min_account_days):
        return _refuse(conn, m, "account too new", f"Launches are open to X accounts older than "
                                                  f"{s.launch_min_account_days} days, ser. Try again later.", **fields)
    wallets = {w.lower() for w in WALLET.findall(text)}
    if len(wallets) != 1:
        return _refuse(conn, m, "no wallet", "Add the wallet that should get the creator fees (one 0x address) "
                                              "and ask again 🫎", **fields)
    if not m.get("images"):
        return _refuse(conn, m, "no image", "Attach an image for the coin and ask again 🫎", **fields)
    pair_sym = asked_pair(text)
    if pair_sym == "ETH":
        pair_sym = None
    pair_addr = pair_token(pair_sym) if pair_sym else None
    if pair_sym and not pair_addr:
        return _refuse(conn, m, "unknown pair", f"I can pair coins with ETH or a Robinhood stock like TSLA, NVDA, AAPL "
                                                f"or SPY, but not ${pair_sym}. Pick another and ask again 🫎", **fields)
    if not d.approve:
        return _refuse(conn, m, "muse declined: " + d.reason, (d.reason or "I can't launch that one.")[:200], **fields)
    if not name or not SYMBOL.match(sym) or sym.lower() not in text.lower() or name.lower() not in text.lower():
        return _refuse(conn, m, "unclear name or ticker", "Give me a name and a ticker (letters and numbers, up to 10) "
                                                          "and I'll launch it 🫎", **fields)
    if sym == "MOOSE":  # any name goes, except a copy of MOOSE's own coin
        return _refuse(conn, m, "reserved ticker", "$MOOSE is my own coin, so I can't launch another one. Pick another "
                                                   "ticker 🫎", **fields)
    if _spent_24h(conn) + EST_COST_WEI > int(s.launch_daily_cap_eth * 10**18):
        return _refuse(conn, m, "daily budget used", "Launches are full for today. Ask again tomorrow 🫎", **fields)
    socials = {k: getattr(d, k) for k in ("twitter", "telegram", "discord", "website", "farcaster") if getattr(d, k)}
    conn.execute("INSERT INTO launch_requests(x_post_id,author_id,handle,created_at,status,name,symbol,description,logo,"
                 "socials,recipient,pair_symbol,pair_token) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (m["id"], m.get("author_id") or "", author.get("username"), utcnow(), "approved", name, sym,
                  d.description[:280], m["images"][0], json.dumps(socials), wallets.pop(), pair_sym, pair_addr))
    journal_append(conn, "launchpad_approved", {"x_post_id": m["id"], "handle": author.get("username"),
                                                "pair": pair_sym, **fields})
    return "approved"


def launchpad_once(ctx: Any, launcher: Launcher | None = None) -> dict[str, Any]:
    s, conn = ctx.settings, ctx.conn
    if not s.launchpad_enabled:
        return {"launchpad": "off"}
    launcher = launcher or Launcher(s, ctx.rpc, ctx.pons, conn)
    launched = handed = 0
    for r in rows(conn, "SELECT * FROM launch_requests WHERE status='approved' ORDER BY id LIMIT 3"):
        launched += _launch(ctx, launcher, r)
    for r in rows(conn, "SELECT * FROM launch_requests WHERE status='launched' ORDER BY id"):
        try:
            handed += _maybe_hand_over(ctx, launcher, r)
        except (LaunchBlocked, Exception) as exc:  # one coin's hiccup never stops the others; retried next loop
            log.warning("hand-over check failed for %s: %s", r["token"], exc)
    try:
        if launcher.escrow_balance() > 0:  # swept fees refill the launch budget
            launcher.claim()
    except (LaunchBlocked, Exception) as exc:
        log.warning("escrow claim failed: %s", exc)
    return {"launched": launched, "handed_over": handed}


EXPLORER = "https://explorer.mainnet.chain.robinhood.com"


def announce_key(r: dict[str, Any]) -> str:
    return f"launch_announce:{r['id']}"


def _by(r: dict[str, Any]) -> str:
    return f"@{r['handle']}" if r.get("handle") else "its creator"


def _launch(ctx: Any, launcher: Launcher, r: dict[str, Any]) -> int:
    """Announce, launch, then post the receipt. The coin's X link is written onchain at launch and can't change,
    so MOOSE's announcement goes out first and the coin links to it (unless they gave their own link)."""
    conn = ctx.conn
    key = announce_key(r)
    ann = conn.execute("SELECT status, x_post_id FROM posts WHERE idempotency_key=?", (key,)).fetchone()
    pair = r.get("pair_symbol")
    if ann is None:
        desc = f"\n\n{r['description']}" if r["description"] else ""
        creator = f"\nCreator: @{r['handle']}" if r["handle"] else ""
        paired = f"\nPaired with {pair}" if pair else ""
        enqueue(conn, Draft("launch_announce", f"🚀 New on the MOOSE launchpad\n\n${r['symbol']} · {r['name']}{creator}"
                                               f"{paired}{desc}\n\nLaunching now. CA and receipt below 👇",
                            r["token"] or f"launch:{r['id']}", key))
        return 0
    if ann["status"] in ("pending", "sending"):
        return 0  # the coin links to that post: launch once it's out
    posted = ann["status"] == "sent" and ann["x_post_id"]
    conn.execute("UPDATE launch_requests SET status='launching' WHERE id=?", (r["id"],))
    socials = json.loads(r["socials"] or "{}")
    if not socials.get("twitter") and posted:
        socials["twitter"] = f"https://x.com/MooseByMuse/status/{ann['x_post_id']}"
    credit = f"Created by @{r['handle']}, launched via @MooseByMuse" if r["handle"] else "Launched via @MooseByMuse"
    own = (r["description"] or "").strip().rstrip(".").strip()
    desc = f"{own}. {credit}" if own else credit  # one full stop, whatever they typed
    m = {"id": r["x_post_id"], "author_id": r["author_id"]}
    try:
        out = launcher.launch(name=r["name"], symbol=r["symbol"], logo=r["logo"], description=desc, socials=socials,
                              ref=r["x_post_id"], pair_token=r.get("pair_token") or ZERO_ADDRESS,
                              recipient=r["recipient"] if pair else None)  # stock pair: fees are theirs from the start
    except LaunchBlocked as exc:
        conn.execute("UPDATE launch_requests SET status='failed', reason=? WHERE id=?", (str(exc)[:300], r["id"]))
        sorry = f"❌ ${r['symbol']} didn't launch\n\nSomething went wrong on my side and no coin was made. Sorry fren."
        if posted:
            enqueue(conn, Draft("launch_receipt", sorry, f"launch:{r['id']}", f"launch_receipt:{r['id']}", reply_to=key))
        _reply(conn, m, "Couldn't launch that one right now, something went wrong on my side. Sorry fren, "
                        "try again later 🫎", kind="launch_failed")
        return 0
    now = utcnow()
    conn.execute("UPDATE launch_requests SET status=?, token=?, curve=?, tx_hash=?, cost_wei=?, earned_wei='0', "
                 "launched_at=?, handed_at=?, socials=? WHERE id=?",
                 ("handed_over" if pair else "launched", out.token, out.curve, out.tx_hash, str(out.cost_wei), now,
                  now if pair else None, json.dumps(socials), r["id"]))
    fees = f"straight to {_by(r)}, paid in {pair}" if pair else f"{_by(r)}, once the launch is paid back"
    receipt = (f"✅ ${r['symbol']} is live on Robinhood Chain\n\nCA: {out.token}\nTx: {EXPLORER}/tx/{out.tx_hash}\n"
               + (f"Pair: {pair}\n" if pair else "") + "\n"
               f"Launch cost: {out.cost_wei / 10**18:.5f} ETH (paid by MOOSE)\n"
               f"Creator fees: {fees}")
    if posted:
        enqueue(conn, Draft("launch_receipt", receipt, out.token, f"launch_receipt:{r['id']}", reply_to=key))
    link = f"https://x.com/MooseByMuse/status/{ann['x_post_id']}" if posted else f"https://gmgn.ai/robinhood/token/{out.token}"
    yours = (f"Paired with {pair}. Its creator fees go straight to your wallet, paid in {pair}." if pair else
             "Creator fees go to your wallet once the coin pays back its ~$2 launch.")
    _reply(conn, m, f"🚀 ${r['symbol']} is live\n\nCA: {out.token}\n\n{yours}\n\n{link}")
    # they asked before and it was refused (no image, say): the answer goes under that first post too
    for old in rows(conn, "SELECT x_post_id FROM launch_requests WHERE author_id=? AND upper(symbol)=? AND "
                          "status='refused' AND id<?", (r["author_id"], r["symbol"].upper(), r["id"])):
        _reply(conn, {"id": old["x_post_id"], "author_id": r["author_id"]},
               f"🚀 ${r['symbol']} is live now\n\nCA: {out.token}\n\nReceipt: {link}", kind="launch_followup")
    return 1


def _maybe_hand_over(ctx: Any, launcher: Launcher, r: dict[str, Any]) -> int:
    """Hand the coin's creator fees to its requester once they've paid back the launch."""
    conn = ctx.conn
    cost, earned = int(r["cost_wei"] or 0), int(r["earned_wei"] or 0)
    rec = ctx.pons.launch_record(r["token"])
    if rec.phase == Phase.NOT_GRADUATED and earned < cost:  # sweep only until the launch is paid back, never again
        if earned + launcher.pending_creator_wei(r["curve"]) < cost:
            return 0
        earned += launcher.sweep(r["curve"], ref=r["x_post_id"])
        conn.execute("UPDATE launch_requests SET earned_wei=? WHERE id=?", (str(earned), r["id"]))
        if earned < cost:
            return 0
    # graduated: the coin traded past 4.2 ETH on its curve, its fees long since covered the launch
    if (r["recipient"] or "").lower() == (launcher.wallet or "").lower():  # MOOSE's own coin: the fees stay here
        conn.execute("UPDATE launch_requests SET status='handed_over', handed_at=? WHERE id=?", (utcnow(), r["id"]))
        return 0
    tx = launcher.hand_over(r["token"], r["recipient"])
    conn.execute("UPDATE launch_requests SET status='handed_over', handed_at=? WHERE id=?", (utcnow(), r["id"]))
    journal_append(conn, "launchpad_handed_over", {"token": r["token"], "to": r["recipient"], "tx_hash": tx,
                                                   "earned_wei": str(earned), "cost_wei": str(cost)}, ref=r["token"])
    _reply(conn, {"id": r["x_post_id"], "author_id": r["author_id"]},
           f"🤝 ${r['symbol']} has paid back its launch\n\nFrom now on its creator fees go straight to your wallet.",
           kind="launch_handed")
    return 1
