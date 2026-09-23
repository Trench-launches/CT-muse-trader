import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
import pytest

from ct_agent.chain.pons import PonsV2
from ct_agent.chain.rpc import RpcClient
from ct_agent.db import journal_recent, rows
from ct_agent.sources.addresses import chain_hint, extract_evm_addresses
from ct_agent.sources.news import ingest_feed
from ct_agent.sources.store import (SourceIn, add_source, evidence_for_token, independent_origins,
                                    verify_leads)
from ct_agent.sources.twitch import (TwitchSignatureError, handle_eventsub, ingest_transcript,
                                     parse_twitch_ts, verify_signature)
from ct_agent.sources.x_api import XBudgetExceeded, XClient

from .fakes import FACTORY, FakeChain, FakeToken, addr

A = "0x47a360fc5e39aaea67056f380b22428d69c794d4"


def test_extracts_typed_and_spoken_addresses():
    assert extract_evm_addresses(f"ape {A} now, also {A.upper().replace('0X', '0x')}") == [A]
    spoken = "the ca is zero x " + " ".join("four seven a three six zero f c five e three nine a a e a "
                                             "six seven zero five six f three eight zero b two two four "
                                             "two eight d six nine c seven nine four d four".split())
    assert extract_evm_addresses(spoken) == [A]
    assert extract_evm_addresses("0x123 too short") == []
    assert chain_hint("new pons launch on robinhood") == "robinhood"
    assert chain_hint("this is on solana") == "solana"


def test_five_reposts_of_one_rumor_is_one_origin(db):
    rumor = "https://x.com/leaker/status/1"
    for i in range(5):
        add_source(db, SourceIn(kind="x", origin_id=str(i), origin_url=f"https://x.com/t{i}/status/{i}",
                                excerpt="big partnership coming", confidence="rumor", claim_key="partnership",
                                cites_url=rumor if i % 2 else rumor.replace("x.com", "twitter.com")))
    assert independent_origins(db, "partnership") == 1
    add_source(db, SourceIn(kind="news", origin_id="n1", origin_url="https://robinhood.com/newsroom/p",
                            excerpt="official", confidence="first_party", claim_key="partnership",
                            cites_url="https://robinhood.com/newsroom/p"))
    assert independent_origins(db, "partnership") == 2


def test_leads_only_link_after_onchain_verification(db):
    ch = FakeChain()
    t = FakeToken(token=addr(0xA1), curve=addr(0xC1), deployer=addr(0xD1))
    ch.launch(t, 10)
    pons = PonsV2(RpcClient("http://fake", transport=ch.transport(), retries=0), FACTORY)
    add_source(db, SourceIn(kind="twitch_chat", origin_id="m1", excerpt=f"CA {t.token.lower()} on pons",
                            confidence="sentiment"))
    add_source(db, SourceIn(kind="twitch_chat", origin_id="m2", excerpt=f"fake {addr(0xBEEF).lower()}",
                            confidence="sentiment"))
    add_source(db, SourceIn(kind="x", origin_id="m3", excerpt=f"{t.token.lower()} on solana", confidence="rumor"))
    assert evidence_for_token(db, t.token) == []  # nothing linked before verification
    res = {r["id"]: r["status"] for r in verify_leads(db, pons)}
    assert list(res.values()) == ["verified", "rejected", "rejected"]
    assert len(evidence_for_token(db, t.token)) == 1
    assert any(j["kind"] == "lead_rejected" for j in journal_recent(db))


def test_news_feed_keeps_link_and_time(db):
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Wire</title>
      <item><title>Robinhood Chain update</title><link>https://example.com/a</link>
      <pubDate>Tue, 22 Sep 2026 10:00:00 GMT</pubDate><description>details</description></item>
    </channel></rss>"""
    assert ingest_feed(db, "https://example.com/feed|first_party", fetch=lambda u: rss) == 1
    assert ingest_feed(db, "https://example.com/feed|first_party", fetch=lambda u: rss) == 0  # dedupe
    s = rows(db, "SELECT * FROM sources")[0]
    assert s["origin_url"] == "https://example.com/a" and s["published_at"] == "2026-09-22T10:00:00+00:00"
    assert s["confidence"] == "first_party"


def _signed(secret, body, mtype="notification", msg_id="abc", ts=None):
    ts = ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
    sig = "sha256=" + hmac.new(secret.encode(), (msg_id + ts).encode() + body, hashlib.sha256).hexdigest()
    return {"Twitch-Eventsub-Message-Id": msg_id, "Twitch-Eventsub-Message-Timestamp": ts,
            "Twitch-Eventsub-Message-Signature": sig, "Twitch-Eventsub-Message-Type": mtype}


def test_twitch_eventsub_verification_chat_and_replay(db):
    secret = "s3cret-s3cret"
    challenge = {"challenge": "pogchamp", "subscription": {"type": "stream.online"}}
    b = json.dumps(challenge).encode()
    assert handle_eventsub(db, secret, _signed(secret, b, "webhook_callback_verification", "c1"), b, challenge) \
        == (200, "pogchamp")
    chat = {"subscription": {"type": "channel.chat.message"},
            "event": {"broadcaster_user_login": "streamer", "chatter_user_login": "viewer", "message_id": "m9",
                      "message": {"text": f"buy {A}"}}}
    b = json.dumps(chat).encode()
    hdr = _signed(secret, b, msg_id="n1")
    assert handle_eventsub(db, secret, hdr, b, chat)[0] == 204
    assert handle_eventsub(db, secret, hdr, b, chat)[0] == 204  # duplicate ignored
    src = rows(db, "SELECT * FROM sources")
    assert len(src) == 1 and src[0]["confidence"] == "sentiment"
    assert rows(db, "SELECT address FROM leads")[0]["address"] == A
    with pytest.raises(TwitchSignatureError):
        handle_eventsub(db, secret, {**hdr, "Twitch-Eventsub-Message-Signature": "sha256=00"}, b, chat)
    with pytest.raises(TwitchSignatureError):
        verify_signature(secret, _signed(secret, b, ts="2020-01-01T00:00:00Z"), b)


def test_twitch_timestamp_nanoseconds():
    assert parse_twitch_ts("2026-09-23T10:11:12.634234626Z").microsecond == 634234


def test_transcript_intake_creates_lead(db):
    ingest_transcript(db, channel="streamer", url="https://www.twitch.tv/videos/1", offset_s=125,
                      excerpt=f"contract is {A}")
    s = rows(db, "SELECT * FROM sources")[0]
    assert s["origin_url"].endswith("?t=125s") and s["kind"] == "twitch_transcript"
    assert rows(db, "SELECT status FROM leads")[0]["status"] == "unverified"


def test_x_search_budget_and_citations(db):
    def h(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer app"
        return httpx.Response(200, json={
            "data": [{"id": "1", "text": f"{A} mooning", "author_id": "u1", "created_at": "2026-09-23T00:00:00Z",
                      "entities": {"urls": [{"expanded_url": "https://news.example/story"}]}}],
            "includes": {"users": [{"id": "u1", "username": "trader1"}]}})
    x = XClient(bearer_token="app", monthly_read_budget=15, conn=db, transport=httpx.MockTransport(h))
    assert x.ingest_search(db, "pons", max_results=10) == 1
    s = rows(db, "SELECT * FROM sources")[0]
    assert s["origin_url"] == "https://x.com/trader1/status/1" and s["cites_url"] == "https://news.example/story"
    assert x.reads_used() == 1  # charged per post returned, not per post requested
    x.budget = 10
    with pytest.raises(XBudgetExceeded):  # 1 used + up to 10 more would exceed 10
        x.search_recent("pons", max_results=10)
