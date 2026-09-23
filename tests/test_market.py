import httpx

from ct_agent.market.dex import MarketData
from ct_agent.market.money import QuoteAmount

GENUINE = "0x6c96006423d90e4aca950b72e50b143db065917b28537c7ca8fe7cd7d6e30022"
LOOKALIKE = "0x2ee40bb64c58a0da5500b19bfa5a175ffbc18ed9b890d66776cf80e5f9f9163d"


def _handler(ds_pairs, gt_status=404, gt_body=None):
    def h(req: httpx.Request) -> httpx.Response:
        if "dexscreener" in req.url.host:
            return httpx.Response(200, json=ds_pairs)
        if "geckoterminal" in req.url.host:
            return httpx.Response(gt_status, json=gt_body or {})
        if "coingecko" in req.url.host:
            return httpx.Response(200, json={"ethereum": {"usd": 4000.0}})
        return httpx.Response(404)
    return httpx.MockTransport(h)


def test_picks_genuine_pool_and_ignores_lookalike():
    pairs = [
        {"pairAddress": LOOKALIKE, "volume": {"h1": 999999}, "liquidity": {"usd": 999999}},
        {"pairAddress": GENUINE, "priceUsd": "0.0004", "volume": {"h1": 29690.37, "h24": 3871937.9},
         "liquidity": {"usd": 43268.8}, "txns": {"h1": {"buys": 40, "sells": 30}}},
    ]
    md = MarketData(transport=_handler(pairs))
    s = md.genuine_pool_stats("0xtoken", GENUINE)
    assert s and s.source == "dexscreener" and s.volume_h1_usd == 29690.37 and s.txns_h1 == 70


def test_returns_none_when_only_lookalikes_exist():
    md = MarketData(transport=_handler([{"pairAddress": LOOKALIKE, "volume": {"h1": 5e6}}]))
    assert md.genuine_pool_stats("0xtoken", GENUINE) is None


def test_geckoterminal_fallback():
    body = {"data": {"attributes": {"address": GENUINE, "volume_usd": {"h1": "23981.5", "h24": "1"},
                                    "reserve_in_usd": "46630.9", "base_token_price_usd": "0.1"}}}
    md = MarketData(transport=_handler([], 200, body))
    s = md.genuine_pool_stats("0xtoken", GENUINE)
    assert s and s.source == "geckoterminal" and s.liquidity_usd == 46630.9


def test_quote_amount_never_mislabels_units():
    q = QuoteAmount(raw=4_200_000_000_000_000_000, decimals=18, symbol="ETH")
    assert q.label() == "4.2 ETH"
    assert q.label(4000.0) == "4.2 ETH (~$16,800 indicative)"
    assert MarketData(transport=_handler([])).eth_usd() == 4000.0
