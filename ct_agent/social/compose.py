"""Post drafting in the CT Agent voice.

Policy baked in:
  * paper trades always say PAPER and never read as onchain;
  * contract address, size, entry and a concrete risk on every trade post;
  * losses and corrections get posted too;
  * holdings of the discussed token (and the agent's own token) disclosed;
  * no replies, no trend-jacking: posts only cover the agent's own
    decisions, trades and verified sources.
"""
from __future__ import annotations

from dataclasses import dataclass

LIMIT = 280


@dataclass
class Draft:
    kind: str
    body: str
    ref: str
    idempotency_key: str
    reply_to: str | None = None  # idempotency_key of the post to thread under


def _fit(parts_required: list[str], optional: list[str]) -> str:
    """Keep required parts, add optional ones only while under the limit,
    trimming the first optional part if needed."""
    body = " ".join(p for p in parts_required if p)
    for p in optional:
        if not p:
            continue
        candidate = f"{body} {p}"
        if len(candidate) <= LIMIT:
            body = candidate
            continue
        room = LIMIT - len(body) - 2
        if room > 40:
            body = f"{body} {p[:room].rsplit(' ', 1)[0]}…"
        break
    return body[:LIMIT]


def _s(text: str) -> str:
    """One sentence ending in exactly one full stop (the AI's text may already end in one)."""
    text = text.strip().rstrip(".").strip()
    return f"{text}." if text and text[-1] not in "!?" else text


def usd_compact(v: float | None) -> str:
    """$42.1K / $1.23M style, or '' if unknown."""
    if not v:
        return ""
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= div:
            return f"${v / div:.3g}{suf}"
    return f"${v:.0f}"


def _short(addr: str) -> str:
    return addr  # full address: readers must be able to verify the exact contract


def _mode_tag(mode: str) -> str:
    return "PAPER BUY" if mode == "paper" else "BOUGHT"


def disclosure(*, holds: bool, mode: str, own_token: bool) -> str:
    if own_token:
        return "Disclosure: this is my own project token."
    if holds:
        return "I hold this (paper position)." if mode == "paper" else "I hold this."
    return ""


def trade_entry(*, mode: str, symbol: str, token: str, size: str, entry: str, catalyst: str, progress: float | None,
                momentum: str, risk: str, position_id: int, own_token: bool = False, mc: str = "") -> Draft:
    stage = f"while the curve was {progress * 100:.0f}% complete" if progress is not None and progress < 1 \
        else "after migration to its Pons V4 pool"
    at_mc = f" at {mc} MC" if mc else ""
    required = [f"{_mode_tag(mode)} ${symbol} on Robinhood Chain{at_mc}: {size} @ {entry}. CA {_short(token)}."]
    optional = [
        f"Risk: {_s(risk)}",
        f"Saw {catalyst} {stage}" + (f", {momentum}." if momentum else "."),
        "Not onchain, simulated fill." if mode == "paper" else "",
        "I'll log the exit too.",
        disclosure(holds=False, mode=mode, own_token=own_token),
    ]
    # Risk goes first among optional parts so it survives trimming.
    return Draft("trade_entry", _fit(required, optional), token, f"entry:{mode}:{position_id}")


def position_update(*, mode: str, symbol: str, token: str, pnl_pct: float, price: str, milestone: float, stop_pct: float,
                    target_pct: float, position_id: int, mc: str = "") -> Draft:
    """A milestone update, posted as a reply under the entry post."""
    now = f"now {mc} MC ({price})" if mc else f"now {price}"
    body = (f"${symbol} {pnl_pct:+.0f}% since the call, {now}. Still holding. "
            f"Exit at -{stop_pct:.0f}% or +{target_pct:.0f}%.")
    if mode == "paper":
        body += " Paper position."
    return Draft("update", body, token, f"update:{mode}:{position_id}:{milestone:+g}",
                 reply_to=f"entry:{mode}:{position_id}")


def trade_exit(*, mode: str, symbol: str, token: str, pnl_pct: float, exit_price: str, reason: str,
               position_id: int) -> Draft:
    verb = "PAPER SOLD" if mode == "paper" else "SOLD"
    result = f"{'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%"
    required = [f"{verb} ${symbol} @ {exit_price}, {result}. CA {_short(token)}."]
    optional = [f"Why: {_s(reason)}", "Loss logged, not hidden." if pnl_pct < 0 else "",
                "Simulated, not onchain." if mode == "paper" else ""]
    return Draft("trade_exit", _fit(required, optional), token, f"exit:{mode}:{position_id}",
                 reply_to=f"entry:{mode}:{position_id}")


def pass_note(*, symbol: str, token: str, reason: str, decision_id: int) -> Draft:
    return Draft("pass", _fit([f"Passing on ${symbol} ({_short(token)})."], [_s(reason)]), token,
                 f"pass:{decision_id}")


def watch_note(*, symbol: str, token: str, thesis: str, risk: str, decision_id: int, holds: bool = False,
               mode: str = "paper", mc: str = "") -> Draft:
    at_mc = f" at {mc} MC" if mc else ""
    return Draft("watch", _fit([f"Watching ${symbol} on Robinhood Chain{at_mc}. CA {_short(token)}."],
                               [_s(thesis), f"What would make me wrong: {_s(risk)}",
                                disclosure(holds=holds, mode=mode, own_token=False)]), token, f"watch:{decision_id}")


def correction(*, original_post_id: str, text: str, key: str) -> Draft:
    return Draft("correction", _fit([f"Correction to https://x.com/i/status/{original_post_id}:"], [text]), "",
                 f"correction:{key}")


def weekly_scorecard(*, week: str, mode: str, trades: int, wins: int, losses: int, pnl_quote: float,
                     quote_symbol: str, best: str, worst: str, own_token_line: str = "") -> Draft:
    label = "Paper scorecard" if mode == "paper" else "Scorecard"
    required = [f"{label} {week}: {trades} closed, {wins}W/{losses}L, net {pnl_quote:+.4f} {quote_symbol}."]
    optional = [f"Best: {best}." if best else "", f"Worst: {worst}." if worst else "", own_token_line,
                "Every fill is in the public journal."]
    return Draft("scorecard", _fit(required, optional), "", f"scorecard:{mode}:{week}")
