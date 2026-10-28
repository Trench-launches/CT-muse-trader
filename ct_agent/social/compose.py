"""Post drafting in the CT Agent voice.

Policy baked in:
  * paper trades always say TEST / Testing Phase and never read as onchain;
  * the $TICKER (no contract address), size, entry and a concrete risk on every trade post;
  * losses and corrections get posted too;
  * holdings of the discussed token (and the agent's own token) disclosed;
  * no replies, no trend-jacking: posts only cover the agent's own
    decisions, trades and verified sources.
"""
from __future__ import annotations

from dataclasses import dataclass

LIMIT = 276  # X counts an emoji as 2 characters: the headline emoji and a little slack stay under 280


@dataclass
class Draft:
    kind: str
    body: str
    ref: str
    idempotency_key: str
    reply_to: str | None = None  # idempotency_key of the post to thread under
    media: tuple[str, ...] = ()  # image files in ct_agent/social/media to attach


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


def _sol(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}{abs(v):.4f} SOL"


def _bag(mode: str) -> str:
    return "test bag" if mode == "paper" else "bag"


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
    return "TEST BUY" if mode == "paper" else "BOUGHT"


def disclosure(*, holds: bool, mode: str, own_token: bool) -> str:
    if own_token:
        return "Disclosure: this is my own project token."
    if holds:
        return "I hold this (testing phase)." if mode == "paper" else "I hold this."
    return ""


# House style for every post: an emoji headline with the $TICKER and the key number, a block of labelled
# facts (one per line), then the thesis or reason in words. Every part that can run long has shorter
# fallbacks, so a post never gets cut off mid-sentence.

def _block(lines: list[str]) -> str:
    return "\n".join(x for x in lines if x)


def _alts(label: str, text: str, cuts: tuple[int, ...]) -> list[str]:
    """`label: text` at full length, then shortened at a word, then dropped."""
    text = " ".join((text or "").split())
    if not text:
        return [""]
    out = [f"{label}{_s(text)}"] + [f"{label}{_s(_phrase(text, n))}" for n in cuts if len(text) > n]
    return out + [""]


def trade_entry(*, mode: str, symbol: str, token: str, size: str, entry: str, catalyst: str, progress: float | None,
                momentum: str, risk: str, position_id: int, own_token: bool = False, mc: str = "",
                plan: str = "") -> Draft:
    """A clean buy post: what, where it was bought, how much, the exit plan and why. Grok's longer read on the
    coin follows as a reply, so the buy itself stays short."""
    head = f"🟢 {'Test buy' if mode == 'paper' else 'Bought'} ${symbol}"
    facts = _block([f"Entry: {mc} MC" if mc else f"Entry: {entry}", f"Size: {size}", f"Plan: {plan}" if plan else ""])
    why = ["Why: smart-money call on Solana."] if catalyst.startswith("a smart-money call") \
        else _alts("Why: ", catalyst, (140, 100, 60))
    tail = _block(["Testing phase, simulated fill, not onchain." if mode == "paper" else "",
                   disclosure(holds=False, mode=mode, own_token=True) if own_token else ""])
    body = _post([([head], None), ([facts], None), (why, 1), ([tail], None)])
    return Draft("trade_entry", body, token, f"entry:{mode}:{position_id}")


def buy_read(*, symbol: str, token: str, key: str, reply_to: str, why: str = "", reasons: list[str] | None = None,
             risk: str = "") -> Draft:
    """Grok's reasoning for a buy, as a reply under the BOUGHT post. `why` is the thesis (the story and why it
    can catch on) and leads; `reasons` are bullets of onchain proof; then the biggest risk. To fit one post the
    bullets shrink and drop first, and the thesis is only shortened as a last resort."""
    why = " ".join((why or "").split())
    reasons = [" ".join(str(r).split()) for r in (reasons or []) if str(r).strip()][:3]
    risk = " ".join((risk or "").split())

    def build(n: int, cut: int, why_cut: int) -> str:
        parts = [f"🧠 Why I bought ${symbol}"] + ([_cap(_s(_phrase(why, why_cut)))] if why else [])
        if n:
            parts.append("\n".join(f"• {_cap(_phrase(r, cut))}" for r in reasons[:n]))
        if risk:
            parts.append(f"Risk: {_s(_phrase(risk, min(cut, 100)))}")
        return "\n\n".join(parts)

    for why_cut in (200, 150, 110):
        for n in range(len(reasons), -1, -1):
            for cut in (95, 75, 55):
                body = build(n, cut, why_cut)
                if len(body) <= LIMIT:
                    return Draft("analysis", body, token, key, reply_to=reply_to)
    return Draft("analysis", build(0, 55, 110)[:LIMIT], token, key, reply_to=reply_to)


def position_update(*, mode: str, symbol: str, token: str, pnl_pct: float, price: str, milestone: float, plan: str,
                    position_id: int, mc: str = "", thesis: str = "") -> Draft:
    """A milestone update on an open trade. `plan` says where the exits are now; `thesis` is Grok's updated read
    on the coin (shortened, then dropped, if the post runs long)."""
    head = f"{'📈' if pnl_pct >= 0 else '📉'} ${symbol} {pnl_pct:+.0f}% since entry"
    facts = _block([f"MC now: {mc}" if mc else f"Price now: {price}", f"Plan: {plan.strip().rstrip('.')[:1].lower()}{plan.strip().rstrip('.')[1:]}"])
    hold = _alts("Still holding: ", thesis, (140, 90))[:-1] + ["Still holding."]  # the thesis, or just the fact
    if mode == "paper":
        hold = [h + " Testing phase." for h in hold]
    body = _post([([head], None), ([facts], None), (hold, 1)])
    return Draft("update", body, token, f"update:{mode}:{position_id}:{milestone:+g}",
                 reply_to=f"entry:{mode}:{position_id}")


def take_profit(*, mode: str, symbol: str, token: str, sold_pct: float, at: str, why: str, left_pct: float,
                realized_sol: float, position_id: int, rung: int, thesis: str = "") -> Draft:
    """A take-profit rung: part of the bag sold, the rest still open. `why` is the plan now (target hit, stop
    moved); `thesis` is Grok's read on why the rest is still worth holding (shortened, then dropped)."""
    head = f"💰 Jeeted {sold_pct:.0f}% of my ${symbol} {_bag(mode)}"
    facts = _block([f"Sold at: {at}", f"Realised: {_sol(realized_sol)}", f"Still riding: {left_pct:.0f}%"])
    plan = [f"Plan: {_s(why)}", "Plan: stop moved to entry.", ""] if why else [""]
    hold = _alts("Why I'm holding the rest: ", thesis, (120, 80)) if thesis else [""]
    body = _post([([head], None), ([facts], None), (plan, 0), (hold, 1)])
    return Draft("take_profit", body, token, f"tp:{mode}:{position_id}:{rung}", reply_to=f"entry:{mode}:{position_id}")


def trade_exit(*, mode: str, symbol: str, token: str, pnl_pct: float, pnl_sol: float, sold_pct: float, at: str,
               why: str, position_id: int, last_bit: bool = False, bought: str = "", underestimated: str = "",
               lesson: str = "") -> Draft:
    """Closing the rest of a position. In profit: Jeeted. At a loss: Top blasted, with the honest post-mortem.
    `why` is a clause that reads after "because", e.g. "it hit my -25% stop"."""
    test = " (testing)" if mode == "paper" else ""
    pnl = f"Realised P&L: {_sol(pnl_sol)} ({pnl_pct:+.0f}%)"
    if pnl_sol >= 0:
        what = "the rest of" if last_bit else "all of" if sold_pct >= 99 else f"{sold_pct:.0f}% of"
        head = f"✅ Jeeted {what} ${symbol}{test} · {pnl_pct:+.0f}%"
        facts = [_block([f"Sold at: {at}", f"Why: {_phrase(why, 90)}" if why else "", pnl]),
                 _block([f"Sold at: {at}", pnl])]
        return Draft("trade_exit", _post([([head], None), (facts, 1), (["Fully out."], None)]), token,
                     f"exit:{mode}:{position_id}", reply_to=f"entry:{mode}:{position_id}")
    head = f"🔴 Top blasted ${symbol}{test} · {pnl_pct:+.0f}%"
    facts = [_block([f"Sold at: {at}", f"Why: {_phrase(why, 60)}" if why else "", pnl]), _block([f"Sold at: {at}", pnl])]
    b, u, les = _phrase(bought, 70), _phrase(underestimated, 70), _phrase(lesson, 80)
    story = [x for x in (_block([f"I bought for: {b}." if b else "", f"What I missed: {u}." if u else "",
                                 f"Lesson: {les}." if les else ""]),
                         _block([f"What I missed: {u}." if u else "", f"Lesson: {les}." if les else ""]),
                         _block([f"Lesson: {les}." if les else ""])) if x] + [""]
    return Draft("trade_exit", _post([([head], None), (facts, 2), (story, 1)]), token,
                 f"exit:{mode}:{position_id}", reply_to=f"entry:{mode}:{position_id}")


def pass_note(*, symbol: str, token: str, decision_id: int, narrative: str = "", concern: str = "",
              reconsider_if: str = "", line: str = "") -> Draft:
    """Faded: saw the story, didn't like the trade."""
    n, c, r = _phrase(narrative, 60), _phrase(concern, 70), _phrase(reconsider_if, 60)
    if not (n or c):
        facts = [_s(line), ""] if line else [""]
    else:
        facts = [_block([f"The story: {n}" if n else "", f"The problem: {c}" if c else "",
                         f"Would buy if: {r}" if r else ""]),
                 _block([f"The story: {n}" if n else "", f"The problem: {c}" if c else ""])]
    body = _post([([f"⛔ Passing on ${symbol}"], None), (facts, 1), (["No position."], None)])
    return Draft("pass", body, token, f"pass:{decision_id}")


def watch_note(*, symbol: str, token: str, decision_id: int, narrative: str = "", confirmation: str = "",
               watching: str = "", trigger: str = "", line: str = "") -> Draft:
    """On the radar, no position: what caught the eye, what it needs to see, and the buy condition."""
    n, c = _phrase(narrative, 60), _phrase(confirmation, 60)
    w, t = _phrase(watching, 50), _phrase(trigger, 60)
    if not (n or c or w or t):
        facts = [_s(line), ""] if line else [""]
    else:
        facts = [_block([f"The story: {n}" if n else "", f"Need to see: {c}" if c else "",
                         f"Watching: {w}" if w else "", f"Buy if: {t}" if t else ""]),
                 _block([f"The story: {n}" if n else "", f"Need to see: {c}" if c else "", f"Buy if: {t}" if t else ""]),
                 _block([f"The story: {n}" if n else "", f"Need to see: {c}" if c else ""])]
    body = _post([([f"👀 ${symbol} is on my radar"], None), (facts, 1), (["No position yet."], None)])
    return Draft("watch", body, token, f"watch:{decision_id}")


def correction(*, original_post_id: str, text: str, key: str) -> Draft:
    return Draft("correction", _fit([f"Correction to https://x.com/i/status/{original_post_id}:"], [text]), "",
                 f"correction:{key}")


def weekly_scorecard(*, week: str, mode: str, trades: int, wins: int, losses: int, pnl_quote: float,
                     quote_symbol: str, best: str, worst: str, own_token_line: str = "") -> Draft:
    label = "Testing phase scorecard" if mode == "paper" else "Weekly scorecard"
    facts = _block([f"Trades closed: {trades}", f"Wins / losses: {wins} / {losses}",
                    f"Net P&L: {pnl_quote:+.4f} {quote_symbol}", f"Best: {best}" if best else "",
                    f"Worst: {worst}" if worst else ""])
    body = _post([([f"📊 {label} · {week}"], None), ([facts], None), ([own_token_line, ""], 0),
                  (["Every fill is in the public journal."], None)])
    return Draft("scorecard", body, "", f"scorecard:{mode}:{week}")
