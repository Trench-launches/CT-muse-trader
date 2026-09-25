"""The smart-money scanner feed (+30% trigger) and the Uniswap V4 swap calldata."""
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
from eth_abi import decode

from ct_agent.chain.pons import Phase
from ct_agent.execution import v4
from ct_agent.sources import scanner

ETH_ADDR = "0x0000000000000000000000000000000000000000"
HOOK = "0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044"
TOK = "0x1dA81ca01700000000000000000000000000beef"


def _call(i, token, price, hours_ago):
    ts = int((datetime.now(timezone.utc) - timedelta(hours=hours_ago)).timestamp() * 1000)
    return {"id": i, "token_address": token, "token_symbol": f"C{i}", "alert_price": price, "alert_mcap": 100_000,
            "alert_timestamp": ts, "alert_signals": json.dumps({"positive": ["SM net buy $5k"]}), "source": "nansen_screener"}


def _ctx(settings, db, calls, prices, records):
    def supa(req):
        return httpx.Response(200, json=calls)

    def dex(req):
        toks = req.url.path.rsplit("/", 1)[-1].split(",")
        return httpx.Response(200, json=[{"baseToken": {"address": t}, "pairAddress": f"pool-{t.lower()}",
                                          "priceUsd": str(prices[t.lower()])} for t in toks if t.lower() in prices])

    class Pons:
        def launch_record(self, token):
            if token not in records:
                raise ValueError("execution reverted")
            pair, phase = records[token]
            return SimpleNamespace(exists=True, token=token, pair_token=pair, phase=phase)

        def genuine_pool_id(self, rec):
            return f"pool-{rec.token.lower()}"

    s = settings.model_copy(update={"supabase_url": "https://x.supabase.co", "supabase_key": "k"})
    return SimpleNamespace(settings=s, conn=db, pons=Pons(), web=httpx.Client(transport=httpx.MockTransport(supa)),
                           market=SimpleNamespace(_http=httpx.Client(transport=httpx.MockTransport(dex))))


def test_scanner_only_hands_over_calls_that_ran_30pct_in_their_eth_pool(settings, db):
    a, b, c, d, e = (f"0x{n:040x}" for n in (0xA, 0xB, 0xC, 0xD, 0xE))
    calls = [_call(1, a, 1.0, 1), _call(2, b, 1.0, 1), _call(3, c, 1.0, 1), _call(4, d, 1.0, 1), _call(5, e, 1.0, 5)]
    records = {a: (ETH_ADDR, Phase.POOL_CREATED), b: (ETH_ADDR, Phase.POOL_CREATED),
               c: ("0x" + "11" * 20, Phase.POOL_CREATED), e: (ETH_ADDR, Phase.POOL_CREATED)}
    prices = {a: 1.35, b: 1.10, e: 1.50}
    ctx = _ctx(settings, db, calls, prices, records)
    assert scanner.fetch_new(ctx) == 5
    ready = scanner.refresh(ctx)
    assert [r["token"] for r in ready] == [a, e]  # +35% and +50%; b is only +10%
    st = {r["token"]: dict(r) for r in db.execute("SELECT token, status, note, last_move_pct FROM scanner_calls")}
    assert st[c]["status"] == "skipped" and "stock token" in st[c]["note"]
    assert st[d]["status"] == "skipped" and "not a Pons" in st[d]["note"]
    assert st[b]["status"] == "watching" and round(st[b]["last_move_pct"]) == 10

    # past the window, a call is dropped even if it's up
    db.execute("UPDATE scanner_calls SET called_at=? WHERE token=?",
               ((datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(timespec="seconds"), e))
    assert [r["token"] for r in scanner.refresh(ctx)] == [a]
    # bought calls aren't handed over again
    scanner.mark_bought(db, a.upper().replace("0X", "0x"))  # the engine passes a checksummed address
    assert scanner.refresh(ctx) == []
    facts = scanner.call_facts(dict(db.execute("SELECT * FROM scanner_calls WHERE token=?", (a,)).fetchone()), 30)
    assert facts["signals"] == {"positive": ["SM net buy $5k"]} and facts["move_since_call_pct"] == 35.0


def _unpack(calldata):
    assert calldata.startswith("0x3593564c")  # execute(bytes,bytes[],uint256)
    cmds, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], bytes.fromhex(calldata[10:]))
    assert cmds == bytes([0x10]) and len(inputs) == 1
    actions, params = decode(["bytes", "bytes[]"], inputs[0])
    return actions, params


def test_v4_buy_and_sell_use_the_routers_exact_shape():
    pool = v4.EthPool(TOK, 0, 200, HOOK)
    me = "0x1234567890123456789012345678901234567890"
    actions, params = _unpack(v4.buy_calldata(pool, 10**16, 123, me, int(time.time()) + 60))
    assert actions == bytes([0x07, 0x0B, 0x0E])
    (swap,) = decode([v4.EXACT_IN], params[0])
    assert swap[0] == ETH_ADDR and swap[3:] == (10**16, 123) and swap[2] == ()
    assert swap[1] == ((TOK.lower(), 0, 200, HOOK.lower(), b""),)
    assert decode(["address", "uint256", "bool"], params[1]) == (ETH_ADDR, 0, True)
    assert decode(["address", "address", "uint256"], params[2]) == (TOK.lower(), me, 0)

    actions, params = _unpack(v4.sell_calldata(pool, 5 * 10**18, 7, me, int(time.time()) + 60))
    (swap,) = decode([v4.EXACT_IN], params[0])
    assert swap[0] == TOK.lower() and swap[1][0][0] == ETH_ADDR and swap[3:] == (5 * 10**18, 7)
    assert decode(["address", "uint256", "bool"], params[1]) == (TOK.lower(), 0, True)
    assert decode(["address", "address", "uint256"], params[2]) == (ETH_ADDR, me, 0)
