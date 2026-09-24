"""Auto-replies: the hard filter, the caps, and threading under someone else's post."""
from types import SimpleNamespace

from ct_agent.brain.muse import MuseReply, MuseResult
from ct_agent.db import kv_set
from ct_agent.social.outbox import _next_sendable
from ct_agent.social.replies import clean, replies_once


def test_filter_drops_anything_unsafe_and_strips_tags():
    assert clean("gm fren @someone, MOOSE only buys once a call runs +30%. wagmi") == \
        "gm fren , MOOSE only buys once a call runs +30%. wagmi"
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
        return MuseResult(MuseReply(skip=self.skip, reply=self.text), "", "m", "h")


def _ctx(settings, db, mentions, brain):
    s = settings.model_copy(update={"x_auto_reply_enabled": True, "x_posting_enabled": True, "x_replies_per_day": 3,
                                    "x_replies_per_user_day": 2, "x_mentions_poll_min": 0})
    return SimpleNamespace(settings=s, conn=db, x=_X(mentions), brain=brain)


def test_replies_respect_caps_and_skip_backlog(settings, db):
    ms = [{"id": str(i), "author_id": "fan" if i < 4 else f"u{i}", "text": f"gm moose {i}"} for i in range(1, 7)]
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


def test_muse_can_decline(settings, db):
    kv_set(db, "mentions_since_id", "0")
    kv_set(db, "x_my_user_id", "me")
    ctx = _ctx(settings, db, [{"id": "9", "author_id": "troll", "text": "you suck"}], _Brain("", skip=True))
    assert replies_once(ctx)["declined"] == 1
    assert db.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"] == 0
