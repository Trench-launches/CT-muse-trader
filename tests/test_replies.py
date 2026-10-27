"""Auto-replies: the hard filter, the caps, and threading under someone else's post."""
from types import SimpleNamespace

from ct_agent.brain.grok import GrokReply, GrokResult
from ct_agent.db import kv_set
from ct_agent.social.outbox import _next_sendable
from ct_agent.social.replies import clean, replies_once


def test_filter_drops_anything_unsafe_and_strips_tags():
    assert clean("gm fren @someone, HORIZON only buys once a call runs +30%. wagmi") == \
        "gm fren , HORIZON only buys once a call runs +30%. wagmi"
    for bad in ("this is shit", "buy at pump.fun now", "ca is 0x1234abcd99", "guaranteed 10x ser", "x" * 300, ""):
        assert clean(bad) is None, bad


class _X:
    bearer, can_post = "b", True

    def __init__(self, mentions):
        self._m = mentions

    def my_user_id(self):
        return "me"

    def mentions(self, user_id, since_id=None, max_results=20):
        return self._m


class _Brain:
    def __init__(self, text, skip=False):
        self.text, self.skip, self.seen = text, skip, []

    def reply(self, ctx):
        self.seen.append(ctx)
        return GrokResult(GrokReply(skip=self.skip, reply=self.text), "", "m", "h")


def _ctx(settings, db, mentions, brain):
    s = settings.model_copy(update={"x_auto_reply_enabled": True, "x_posting_enabled": True, "x_replies_per_day": 3,
                                    "x_replies_per_user_day": 2, "x_mentions_poll_min": 0})
    return SimpleNamespace(settings=s, conn=db, x=_X(mentions), brain=brain)


def test_replies_respect_caps_and_skip_backlog(settings, db):
    ms = [{"id": str(i), "author_id": "fan" if i < 4 else f"u{i}", "text": f"gm horizon {i}"} for i in range(1, 7)]
    ms.append({"id": "7", "author_id": "me", "text": "my own post"})
    brain = _Brain("gm fren, LFG")
    ctx = _ctx(settings, db, ms, brain)
    assert replies_once(ctx)["replies"] == "started"  # the first run only sets where to start from
    out = replies_once(ctx)
    # fan gets 2 (per-person cap), then u4 fills the daily cap of 3; our own post is never answered
    assert out["replies_queued"] == 3
    got = db.execute("SELECT ref, reply_to_key FROM posts WHERE kind='reply' ORDER BY id").fetchall()
    assert [r["ref"] for r in got] == ["x_user:fan", "x_user:fan", "x_user:u4"]
    row, parent = _next_sendable(db)
    assert parent == "1"  # posted straight under their post


def test_grok_can_decline(settings, db):
    kv_set(db, "mentions_since_id", "0")
    kv_set(db, "x_my_user_id", "me")
    ctx = _ctx(settings, db, [{"id": "9", "author_id": "troll", "text": "you suck"}], _Brain("", skip=True))
    assert replies_once(ctx)["declined"] == 1
    assert db.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"] == 0


class _Writer:
    def __init__(self, text):
        self.text, self.calls = text, 0

    def musing(self, ctx):
        from ct_agent.brain.grok import GrokMusing
        self.calls += 1
        return GrokResult(GrokMusing(tweet=self.text), "", "m", "h")


def test_musings_every_half_hour_never_eat_the_trade_posts_room(settings, db):
    from ct_agent.social.musings import musings_once
    s = settings.model_copy(update={"x_musings_enabled": True, "x_posting_enabled": True, "x_daily_post_cap": 40,
                                    "x_cadence_min": 0})  # musings on their own timer
    brain = _Writer("liquidity leaves quietly and returns loudly.")
    ctx = SimpleNamespace(settings=s, conn=db, x=_X([]), brain=brain)
    assert musings_once(ctx) == {"musing": "queued"}
    assert musings_once(ctx) == {} and brain.calls == 1  # not again within 30 min
    db.execute("UPDATE posts SET status='sent', sent_at=created_at")
    kv_set(db, "musing_last_at", "2000-01-01T00:00:00+00:00")
    assert musings_once(ctx) == {"musing": "dropped"}  # the same line twice is never posted
    for i in range(10):  # 11 sent today: 40 cap - 30 reserved for trades = no room
        db.execute("INSERT INTO posts(idempotency_key,kind,body,ref,created_at,status,sent_at) VALUES(?,?,?,?,datetime('now'),'sent',"
                   "strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))", (f"k{i}", "watch", "x", ""))
    kv_set(db, "musing_last_at", "2000-01-01T00:00:00+00:00")
    assert musings_once(ctx)["musing"].startswith("held")


def test_a_reply_under_a_trade_post_gets_the_coins_story_and_todays_rules(settings, db, monkeypatch):
    import json
    import ct_agent.sources.lore as lore
    tok = "0x" + "ab" * 20
    monkeypatch.setattr(lore, "get_lore", lambda ctx, token, look=True: {"description": "AI models without subscriptions"})
    kv_set(db, "mentions_since_id", "0")
    kv_set(db, "x_my_user_id", "me")
    db.execute("INSERT INTO launches(token,curve,deployer,pair_token,launch_config_id,graduation_threshold,block_number,"
               "tx_hash,first_seen,symbol,name) VALUES(?,?,?,?,0,'1',1,'t','t','FRAI','Free AI')",
               (tok, "0x" + "01" * 20, "0x" + "02" * 20, "0x" + "00" * 20))
    db.execute("INSERT INTO decisions(ts,token,stage,action,confidence,thesis,reasons,risks,source_ids,model,raw) "
               "VALUES('t',?,'migrated','buy',0.6,'internal thesis','[]','[]','[]','m',?)",
               (tok, json.dumps({"public_thesis": "FRAI lets you use AI models without a subscription"})))
    db.execute("INSERT INTO posts(idempotency_key,kind,body,ref,status,x_post_id,created_at) "
               "VALUES('entry:live:9','trade_entry','Bought $FRAI',?,'sent','500','t')", (tok,))
    brain = _Brain("FRAI is AI without the monthly bill, and that story travels")
    s = settings.model_copy(update={"scanner_buy_on_call": True, "pons_scan_enabled": True})
    ctx = _ctx(s, db, [{"id": "501", "author_id": "figz", "text": "whats the thesis here?",
                        "referenced_tweets": [{"type": "replied_to", "id": "500"}]}], brain)
    assert replies_once(ctx)["replies_queued"] == 1
    sent = brain.seen[-1]
    assert sent["coin"]["symbol"] == "FRAI" and sent["coin"]["horizon_thesis"].startswith("FRAI lets you use AI")
    assert sent["coin"]["coins_own_lore"]["description"] == "AI models without subscriptions"
    assert "the moment it lands" in sent["rules"] and "Pons scan" in sent["rules"] and "+30%" not in sent["rules"]
