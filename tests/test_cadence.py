"""The steady timeline: a post at least every x_cadence_min minutes, trades first, the gaps filled in rotation."""
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ct_agent.brain.grok import GrokMusing, GrokResult
from ct_agent.db import rows
from ct_agent.social.cadence import cadence_once

NOW = datetime(2026, 9, 25, 18, 0, tzinfo=timezone.utc)
SITE = {"funnel": {"calls_24h": 93},
        "positions": [{"symbol": "TALIS", "pnl_pct": 92.9}, {"symbol": "FOOMS", "pnl_pct": -46.3}],
        "score": {"taken": 10, "closed": 5, "open": 5, "win_rate": 20.0, "net_sol": -0.0401, "unrealized_sol": 0.0038,
                  "best": {"symbol": "NYMA", "pnl_pct": 27.1}, "worst": {"symbol": "FRAI", "pnl_pct": -79.4}}}


class _Brain:
    goal = "eleven calls read, two earned a buy.\nnext: fifty, with the same filter.\nmore data in, sharper exits out."

    def __init__(self):
        self.goal_ctx = None

    def musing(self, ctx):
        return GrokResult(GrokMusing(tweet="patience is also a position."), "", "m", "h")

    def goals(self, ctx):
        self.goal_ctx = ctx
        return GrokResult(GrokMusing(tweet=self.goal), "", "m", "h")


def _ctx(settings, db, **kw):
    s = settings.model_copy(update={"x_posting_enabled": True, "x_musings_enabled": True, "x_cadence_min": 17, **kw})
    return SimpleNamespace(settings=s, conn=db, x=SimpleNamespace(can_post=True), brain=_Brain())


def _sent(db, kind, at, reply_to=None):
    db.execute("INSERT INTO posts(idempotency_key,kind,body,ref,created_at,status,sent_at,reply_to_key) "
               "VALUES(?,?,?,?,?,'sent',?,?)", (f"{kind}:{at}", kind, "x", "", at, at, reply_to))


def _send_all(db, at):
    db.execute("UPDATE posts SET status='sent', sent_at=? WHERE status='pending'", (at.isoformat(timespec="seconds"),))


def test_a_gap_is_filled_in_rotation_and_every_kind_comes_round(settings, db):
    ctx, t, kinds = _ctx(settings, db), NOW, []
    for _ in range(4):
        got = cadence_once(ctx, SITE, t)["cadence"]
        kinds.append(got)
        _send_all(db, t)
        assert cadence_once(ctx, SITE, t + timedelta(minutes=10)) == {}  # not again inside 17 minutes
        t += timedelta(minutes=17)
    assert kinds == ["scan", "holdings", "musing", "score"]
    bodies = [r["body"] for r in rows(db, "SELECT body FROM posts ORDER BY id")]
    assert "TALIS +93%" in bodies[1] and "Win rate: 20%" in bodies[3]
    for b in bodies:  # X refuses a post with more than one cashtag
        assert len(re.findall(r"\$[A-Za-z][A-Za-z0-9]*", b)) <= 1, b


def test_a_trade_post_counts_as_the_slot_but_replies_do_not(settings, db):
    ctx = _ctx(settings, db)
    _sent(db, "trade_entry", (NOW - timedelta(minutes=5)).isoformat(timespec="seconds"))
    assert cadence_once(ctx, SITE, NOW) == {}  # a trade went out 5 minutes ago
    _sent(db, "reply", (NOW + timedelta(minutes=20)).isoformat(timespec="seconds"), reply_to="x:123")
    assert cadence_once(ctx, SITE, NOW + timedelta(minutes=21))["cadence"] == "scan"  # a reply isn't on the timeline


def test_nothing_new_to_say_skips_to_the_next_kind(settings, db):
    ctx = _ctx(settings, db, x_musings_enabled=False)
    quiet = {"funnel": {}, "positions": [], "score": {"taken": 3, "closed": 3, "open": 0, "win_rate": 0, "net_sol": 0}}
    assert cadence_once(ctx, quiet, NOW)["cadence"] == "score"  # no scan data, no bags, musings off
    _send_all(db, NOW)
    assert cadence_once(ctx, quiet, NOW + timedelta(minutes=17))["cadence"] == "nothing to say"  # same scorecard again


def test_the_scorecard_posts_before_any_trade_has_closed(settings, db):
    ctx = _ctx(settings, db, x_musings_enabled=False)
    first = {"funnel": {}, "positions": [], "score": {"taken": 2, "closed": 0, "open": 2, "win_rate": None,
                                                     "net_sol": 0.0, "best": None, "worst": None}}
    assert cadence_once(ctx, first, NOW)["cadence"] == "score"
    body = rows(db, "SELECT body FROM posts")[0]["body"]
    assert "0 closed, 2 open" in body and "Win rate: none closed yet" in body


def test_a_broken_kind_is_skipped_not_retried_forever(settings, db, monkeypatch):
    import ct_agent.social.cadence as cadence
    monkeypatch.setattr(cadence, "score_post", lambda site: 1 / 0)
    ctx = _ctx(settings, db, x_musings_enabled=False)
    cadence.kv_set(db, "cadence_next", 3)  # the scorecard's turn
    assert cadence_once(ctx, SITE, NOW)["cadence"] == "scan"  # it moves on to the next kind


def test_off_when_the_cadence_is_zero(settings, db):
    assert cadence_once(_ctx(settings, db, x_cadence_min=0), SITE, NOW) == {}


def _history(db):
    """A day like the first live one: 11 calls (2 bought, 2 refused for a transfer tax, 2 never ran), 2 trades."""
    for i, st in enumerate(["bought"] * 2 + ["skipped"] * 2 + ["expired"] * 2 + ["watching"] * 5):
        db.execute("INSERT INTO scanner_calls(id,token,symbol,called_at,call_price_usd,data,status,first_seen,note) "
                   "VALUES(?,?,?,?,1.0,'{}',?,?,?)", (i, f"T{i}", f"C{i}", "2026-09-25T06:00:00+00:00", st,
                                                      "2026-09-25T06:00:00+00:00",
                                                      "unsafe coin: a 1% tax on every transfer" if st == "skipped" else None))
    for tp in (2, 0):
        db.execute("INSERT INTO positions(token,mode,status,quote_symbol,opened_at,entry_quote,entry_tokens,entry_price,"
                   "tp_hits) VALUES(?,?,?,?,?,?,?,?,?)", (f"P{tp}", "paper", "open", "SOL", "2026-09-25T16:49:00+00:00",
                                                         0.01, 1000.0, 1e-5, tp))
    db.execute("INSERT INTO launch_requests(x_post_id,author_id,created_at,status) VALUES('1','a',?,'launched')",
               ("2026-09-25T15:10:00+00:00",))


def test_a_goal_post_comes_round_every_few_hours_with_real_numbers(settings, db):
    _history(db)
    ctx = _ctx(settings, db, x_musings_enabled=False, x_goals_enabled=True)
    import ct_agent.social.cadence as cadence
    cadence.kv_set(db, "cadence_next", 4)  # the goal post's turn
    early = {**SITE, "score": {**SITE["score"], "closed": 0}}
    assert cadence_once(ctx, early, NOW)["cadence"] == "goals"
    body = rows(db, "SELECT body FROM posts WHERE kind='goals'")[0]["body"]
    assert body == "🎯 Where I'm headed\n\n" + _Brain.goal
    f = ctx.brain.goal_ctx  # what Grok was given: only counts from the database
    assert (f["smart_money_calls_read"], f["calls_bought"], f["calls_refused_for_a_tax_on_every_transfer"],
            f["calls_that_never_ran_30pct_in_6h"], f["take_profits_hit"], f["coins_launched_for_people"]) == (11, 2, 2, 2, 2, 1)
    assert f["next_milestones"] == ["my first fully closed trade, so the scorecard has a real win rate",
                                    "50 smart-money calls read (now 11)", "10 take-profits hit (now 2)",
                                    "10 coins launched through the launchpad (now 1)"]
    # one every x_goals_every_hours: next time round it's not due, so the rotation moves on
    _send_all(db, NOW)
    cadence.kv_set(db, "cadence_next", 4)
    assert cadence_once(ctx, SITE, NOW + timedelta(minutes=17))["cadence"] == "scan"


def test_a_goal_post_that_breaks_the_rules_is_dropped(settings, db):
    ctx = _ctx(settings, db, x_musings_enabled=False, x_goals_enabled=True)
    import ct_agent.social.cadence as cadence
    for k, bad in enumerate(("next stop: 10x on $BONK, easy money.", "x" * 241)):  # a cashtag; too long for the header
        ctx.brain.goal, t = bad, NOW + timedelta(minutes=17 * k)
        cadence.kv_set(db, "cadence_next", 4)
        assert cadence_once(ctx, SITE, t)["cadence"] != "goals"  # the rotation moves on
        _send_all(db, t)
    assert rows(db, "SELECT * FROM posts WHERE kind='goals'") == []
    assert len(rows(db, "SELECT * FROM journal WHERE kind='goals_dropped'")) == 2


def test_goal_posts_are_off_unless_switched_on(settings, db):
    ctx = _ctx(settings, db, x_musings_enabled=False)
    import ct_agent.social.cadence as cadence
    cadence.kv_set(db, "cadence_next", 4)
    assert cadence_once(ctx, SITE, NOW)["cadence"] == "scan" and ctx.brain.goal_ctx is None
