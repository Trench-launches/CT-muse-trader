import httpx

from ct_agent.chain.keys import SOL_MINT, USDC_MINT
from ct_agent.market.dex import MarketData
from ct_agent.market.money import QuoteAmount

from .fakes import mint

M = mint(1)


def _handler(ds_pairs, gt_status=404, gt_body=None):
    def h(req: httpx.Request) -> httpx.Response:
        if "dexscreener" in req.url.host:
            return httpx.Response(200, json=ds_pairs)
        if "geckoterminal" in req.url.host:
            return httpx.Response(gt_status, json=gt_body or {})
        if "coingecko" in req.url.host:
            return httpx.Response(200, json={"solana": {"usd": 150.0}})
        return httpx.Response(404)
    return httpx.MockTransport(h)


def _pair(pool, quote, liq, dex="pumpswap", base=M, price="0.0004"):
    return {"pairAddress": pool, "dexId": dex, "baseToken": {"address": base}, "quoteToken": {"address": quote},
            "priceUsd": price, "priceNative": "0.0000026", "volume": {"h1": 29690.37, "h24": 3871937.9},
            "liquidity": {"usd": liq}, "txns": {"h1": {"buys": 40, "sells": 30}}, "marketCap": 400000}


def test_picks_the_deepest_sol_pool():
    pairs = [_pair("usdc-pool", USDC_MINT, 999_999), _pair("small", SOL_MINT, 1_000, dex="raydium"),
             _pair("deep", SOL_MINT, 43_268.8), _pair("other-base", SOL_MINT, 5e6, base=mint(2))]
    s = MarketData(transport=_handler(pairs)).pool_stats(M)
    assert s and s.source == "dexscreener" and s.pool_id == "deep" and s.price_native == 2.6e-06
    assert s.dex_id == "pumpswap" and not s.on_curve and s.market_cap_usd == 400000
    # liquidity, volume and trades are the coin's whole market (all its pools, any quote), not one pool's;
    # another coin's pool (base mint(2)) is not counted
    assert s.liquidity_usd == 999_999 + 1_000 + 43_268.8 and s.txns_h1 == 3 * 70


def test_a_coin_on_the_pump_curve_is_marked_so():
    s = MarketData(transport=_handler([_pair("curve", SOL_MINT, 9_000, dex="pumpfun")])).pool_stats(M)
    assert s.on_curve


def test_geckoterminal_fallback():
    body = {"data": [{"attributes": {"address": "gt-pool", "volume_usd": {"h1": "23981.5", "h24": "1"},
                                     "reserve_in_usd": "46630.9", "base_token_price_usd": "0.1"}}]}
    s = MarketData(transport=_handler([], 200, body)).pool_stats(M)
    assert s and s.source == "geckoterminal" and s.liquidity_usd == 46630.9


def test_prices_take_each_coins_deepest_pair():
    pairs = [_pair("a", SOL_MINT, 10, price="1.0"), _pair("b", SOL_MINT, 1_000, price="2.0")]
    assert MarketData(transport=_handler(pairs)).prices_usd([M, M]) == {M: 2.0}


def test_quote_amount_never_mislabels_units():
    q = QuoteAmount(raw=4_200_000_000, decimals=9, symbol="SOL")
    assert q.label() == "4.2 SOL"
    assert q.label(150.0) == "4.2 SOL (~$630 indicative)"
    assert MarketData(transport=_handler([])).sol_usd() == 150.0


def test_null_fields_from_market_apis_dont_crash():
    def h(req):
        if "dexscreener" in str(req.url):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"data": [{"attributes": {"address": "p", "base_token_price_quote_token": "1e-8",
                                                                   "reserve_in_usd": "5000", "volume_usd": {"h1": None},
                                                                   "transactions": {"h1": {"buys": None, "sells": 3}}}}]})
    st = MarketData(transport=httpx.MockTransport(h)).pool_stats(M)
    assert st.source == "geckoterminal" and st.txns_h1 == 3 and st.price_native == 1e-8
