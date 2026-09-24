"""Post drafting in the CT Agent voice.

Policy baked in:
  * paper trades always say PAPER and never read as onchain;
  * the $TICKER (no contract address), size, entry and a concrete risk on every trade post;
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


def _post(parts: list[tuple[list[str], int | None]]) -> str:
    """Paragraphs separated by blank lines. Each part lists alternatives, longest first ("" drops it).
    While the post is over the limit, the least important part that can still shrink (lowest number;
    None never shrinks) falls back to its next alternative."""
    pick = [0] * len(parts)
    while True:
        body = "\n\n".join(alts[i] for (alts, _), i in zip(parts, pick) if alts[i])
        if len(body) <= LIMIT:
            return body
        can = [(prio, k) for k, ((alts, prio), i) in enumerate(zip(parts, pick)) if prio is not None and i < len(alts) - 1]
        if not can:
            return body[:LIMIT]
        pick[min(can)[1]] += 1


def _phrase(text: str, n: int = 60) -> str:
    """A short phrase from the AI to slot into a sentence: one line, no end punctuation, cut at a word."""
    t = " ".join(str(text or "").split()).rstrip(" .!?;:,")
    if len(t) > n:
        t = t[:n].rsplit(" ", 1)[0].rstrip(" ,;:")
    return t


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _eth(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}{abs(v):.4f} ETH"


def _bag(mode: str) -> str:
    return "paper bag" if mode == "paper" else "bag"


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
    required = [f"{_mode_tag(mode)} ${symbol} on Robinhood Chain{at_mc}: {size} @ {entry}."]
    optional = [
        f"Risk: {_s(risk)}",
        f"Saw {catalyst} {stage}" + (f", {momentum}." if momentum else "."),
        "Not onchain, simulated fill." if mode == "paper" else "",
        "I'll log the exit too.",
        disclosure(holds=False, mode=mode, own_token=own_token),
    ]
    # Risk goes first among optional parts so it survives trimming.
    return Draft("trade_entry", _fit(required, optional), token, f"entry:{mode}:{position_id}")


def position_update(*, mode: str, symbol: str, token: str, pnl_pct: float, price: str, milestone: float, plan: str,
                    position_id: int, mc: str = "") -> Draft:
    """A milestone update, posted as a reply under the entry post. `plan` says where the exits are now."""
    now = f"now {mc} MC ({price})" if mc else f"now {price}"
    body = f"${symbol} {pnl_pct:+.0f}% since the call, {now}. Still holding. {plan}"
    if mode == "paper":
        body += " Paper position."
    return Draft("update", body, token, f"update:{mode}:{position_id}:{milestone:+g}",
                 reply_to=f"entry:{mode}:{position_id}")


def take_profit(*, mode: str, symbol: str, token: str, sold_pct: float, at: str, why: str, left_pct: float,
                realized_eth: float, position_id: int, rung: int) -> Draft:
    """A take-profit rung: part of the bag sold, the rest still open. Reply under the entry post."""
    body = _post([([f"Jeeted {sold_pct:.0f}% of my ${symbol} {_bag(mode)} at {at}."], None),
                  ([_cap(_s(why)), ""] if why else [""], 1),
                  ([f"{left_pct:.0f}% still riding. Realised P&L: {_eth(realized_eth)}."], None)])
    return Draft("take_profit", body, token, f"tp:{mode}:{position_id}:{rung}", reply_to=f"entry:{mode}:{position_id}")


def trade_exit(*, mode: str, symbol: str, token: str, pnl_pct: float, pnl_eth: float, sold_pct: float, at: str,
               why: str, position_id: int, last_bit: bool = False, bought: str = "", underestimated: str = "",
               lesson: str = "") -> Draft:
    """Closing the rest of a position. In profit: Jeeted. At a loss: Top blasted, with the honest post-mortem.
    `why` is a clause that reads after "because", e.g. "it hit my -25% stop"."""
    pnl = f"Realised P&L: {_eth(pnl_eth)} ({pnl_pct:+.0f}%)."
    if pnl_eth >= 0:
        what = f"the last {sold_pct:.0f}%" if last_bit else f"{sold_pct:.0f}%"
        parts = [([f"Jeeted {what} of my ${symbol} {_bag(mode)} at {at}."], None),
                 ([f"Sold because {_phrase(why, 90)}.", ""] if why else [""], 1), ([f"Fully out. {pnl}"], None)]
    else:
        b, u, les = _phrase(bought, 60), _phrase(underestimated, 50), _phrase(lesson, 70)
        story = [x for x in (f"I bought because {b}. Underestimated {u}." if b and u else "",
                             f"Underestimated {u}." if u else "", f"I bought because {b}." if b else "") if x]
        cut_ = f"Cut because {_phrase(why, 60)}." if why else "Cut it."
        parts = [([f"Top blasted ${symbol}{' on paper' if mode == 'paper' else ''}. Entry was poor."], None),
                 (story + [""], 1), ([f"{cut_}\nLesson: {les}.", cut_] if les else [cut_], 2), ([pnl], None)]
    return Draft("trade_exit", _post(parts), token, f"exit:{mode}:{position_id}", reply_to=f"entry:{mode}:{position_id}")


def pass_note(*, symbol: str, token: str, decision_id: int, narrative: str = "", concern: str = "",
              reconsider_if: str = "", line: str = "") -> Draft:
    """Faded: saw the story, didn't like the trade."""
    n, c, r = _phrase(narrative, 45), _phrase(concern, 50), _phrase(reconsider_if, 50)
    why = (f"I see the {n} thesis. Couldn't justify the entry with {c}." if n and c
           else f"Couldn't justify the entry with {c}." if c else _s(line) if line else "")
    again = ([f"Would reconsider if {r}. A good story still needs a good trade.", f"Would reconsider if {r}.", ""] if r
             else ["A good story still needs a good trade.", ""])
    body = _post([([f"Faded ${symbol}."], None), ([why, ""] if why else [""], 2), (again, 1)])
    return Draft("pass", body, token, f"pass:{decision_id}")


def watch_note(*, symbol: str, token: str, decision_id: int, narrative: str = "", confirmation: str = "",
               watching: str = "", trigger: str = "", line: str = "") -> Draft:
    """On the radar, no position: what caught the eye, what it needs to see, and the buy condition."""
    n, c = _phrase(narrative, 45), _phrase(confirmation, 45)
    w, t = _phrase(watching, 40), _phrase(trigger, 50)
    why = (f"{_cap(n)} caught my attention, but I want {c} before buying." if n and c
           else _s(line) if line else "")
    plan = ([f"Watching {w}. If {t}, I take a starter. Until then, happy to wait.",
             f"Watching {w}. If {t}, I take a starter.", f"If {t}, I take a starter.", ""] if w and t
            else [f"Watching {w}. Until then, happy to wait.", ""] if w else [""])
    body = _post([([f"${symbol} is on my radar. No position."], None), ([why, ""] if why else [""], 2),
                  (plan, 1)])
    return Draft("watch", body, token, f"watch:{decision_id}")


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
