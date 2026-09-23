import json
import time
import urllib.parse

import httpx
import pytest

from ct_agent.db import kv_get
from ct_agent.sources.x_api import XClient, XNotConfigured
from ct_agent.sources.x_oauth import KV_KEY, XOAuth2, authorize_url, pkce_pair


def _token_server(log):
    n = {"i": 0}

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/2/oauth2/token":
            form = dict(urllib.parse.parse_qsl(req.content.decode()))
            log.append(form)
            n["i"] += 1
            if form.get("refresh_token") == "dead":
                return httpx.Response(400, json={"error": "invalid_request"})
            return httpx.Response(200, json={"access_token": f"at{n['i']}", "refresh_token": f"rt{n['i']}",
                                             "expires_in": 7200, "token_type": "bearer"})
        if req.url.path == "/2/tweets":
            log.append({"auth": req.headers["authorization"], "text": json.loads(req.content)["text"]})
            return httpx.Response(201, json={"data": {"id": "99", "text": "t"}})
        return httpx.Response(404)
    return httpx.MockTransport(h)


def test_refresh_rotates_and_is_cached(db):
    log = []
    oa = XOAuth2(client_id="cid", client_secret="cs", conn=db, seed_refresh_token="seed", transport=_token_server(log))
    assert oa.access_token() == "at1"
    assert log[0]["grant_type"] == "refresh_token" and log[0]["refresh_token"] == "seed"
    assert kv_get(db, KV_KEY)["refresh_token"] == "rt1"   # rotated token persisted
    assert oa.access_token() == "at1" and len(log) == 1   # cached until expiry
    st = kv_get(db, KV_KEY); st["expires_at"] = time.time() - 1
    from ct_agent.db import kv_set
    kv_set(db, KV_KEY, st)
    assert oa.access_token() == "at2" and log[-1]["refresh_token"] == "rt1"   # uses the rotated one


def test_new_login_replaces_stored_tokens(db):
    log = []
    XOAuth2(client_id="cid", conn=db, seed_refresh_token="seed1", transport=_token_server(log)).access_token()
    XOAuth2(client_id="cid", conn=db, seed_refresh_token="seed2", transport=_token_server(log)).access_token()
    assert log[-1]["refresh_token"] == "seed2"


def test_failed_refresh_keeps_posts_pending(db):
    oa = XOAuth2(client_id="cid", conn=db, seed_refresh_token="dead", transport=_token_server([]))
    with pytest.raises(XNotConfigured, match="x-login"):
        oa.access_token()


def test_post_uses_oauth_token(db):
    log = []
    t = _token_server(log)
    x = XClient(conn=db, transport=t, oauth=XOAuth2(client_id="cid", conn=db, seed_refresh_token="seed", transport=t))
    assert x.can_post
    assert x.create_post("hello").post_id == "99"
    assert log[-1]["auth"] == "Bearer at1"
    assert not XClient(conn=db).can_post


def test_pkce_and_authorize_url():
    v, c = pkce_pair()
    assert 43 <= len(v) <= 128 and "=" not in c
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(authorize_url("cid", c, "st")).query))
    assert q["code_challenge_method"] == "S256" and "offline.access" in q["scope"] and "tweet.write" in q["scope"]
    assert q["redirect_uri"] == "http://127.0.0.1:8765/callback"
