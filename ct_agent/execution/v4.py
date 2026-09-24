"""Uniswap V4 swaps on Robinhood Chain, for graduated Pons coins in their ETH pool.

Calldata is built in the exact shape Robinhood Chain's Universal Router takes, copied from real
swaps on it (not from the Uniswap SDK, whose structs differ here):

  execute(commands=0x10 V4_SWAP, inputs=[abi(actions, params)], deadline)
    buy : SWAP_EXACT_IN(ETH -> coin), SETTLE(ETH, open delta, payer=user), TAKE(coin -> wallet, open delta)
    sell: SWAP_EXACT_IN(coin -> ETH), SETTLE(coin, open delta, payer=user via Permit2), TAKE(ETH -> wallet)

  ExactInputParams = (currencyIn, PathKey[] path, uint256[] maxHopSlippage, uint128 amountIn, uint128 amountOutMin)
  PathKey          = (intermediateCurrency, fee, tickSpacing, hooks, hookData)

Only the one pool Pons seeded is used: currency0 ETH, currency1 the coin, the launch's fee and tick
spacing, and the Pons meme hook. Its id is checked against the launch record before any swap.
"""
from __future__ import annotations

from dataclasses import dataclass

from eth_abi import decode, encode

from ..chain.abi import ZERO_ADDRESS, checksum, selector

V4_SWAP = 0x10
SWAP_EXACT_IN, SETTLE, TAKE = 0x07, 0x0B, 0x0E
OPEN_DELTA = 0  # "whatever the swap left owed": settle exactly what was spent, take exactly what came out
EXACT_IN = "(address,(address,uint24,int24,address,bytes)[],uint256[],uint128,uint128)"
POOL_KEY = "(address,address,uint24,int24,address)"


@dataclass(frozen=True)
class EthPool:
    """The ETH pool Pons seeded for a graduated coin."""
    token: str
    fee: int
    tick_spacing: int
    hook: str

    @property
    def key(self) -> tuple[str, str, int, int, str]:
        return (ZERO_ADDRESS, checksum(self.token), self.fee, self.tick_spacing, checksum(self.hook))


def _swap(currency_in: str, currency_out: str, pool: EthPool, amount_in: int, min_out: int) -> bytes:
    path = [(checksum(currency_out), pool.fee, pool.tick_spacing, checksum(pool.hook), b"")]
    return encode([EXACT_IN], [(checksum(currency_in), path, [], amount_in, min_out)])


def _execute(actions: list[int], params: list[bytes], deadline: int) -> str:
    v4_input = encode(["bytes", "bytes[]"], [bytes(actions), params])
    return selector("execute(bytes,bytes[],uint256)") + encode(
        ["bytes", "bytes[]", "uint256"], [bytes([V4_SWAP]), [v4_input], deadline]).hex()


def buy_calldata(pool: EthPool, amount_in_wei: int, min_tokens_out: int, recipient: str, deadline: int) -> str:
    return _execute([SWAP_EXACT_IN, SETTLE, TAKE], [
        _swap(ZERO_ADDRESS, pool.token, pool, amount_in_wei, min_tokens_out),
        encode(["address", "uint256", "bool"], [ZERO_ADDRESS, OPEN_DELTA, True]),
        encode(["address", "address", "uint256"], [checksum(pool.token), checksum(recipient), OPEN_DELTA]),
    ], deadline)


def sell_calldata(pool: EthPool, tokens_in_wei: int, min_eth_out: int, recipient: str, deadline: int) -> str:
    return _execute([SWAP_EXACT_IN, SETTLE, TAKE], [
        _swap(pool.token, ZERO_ADDRESS, pool, tokens_in_wei, min_eth_out),
        encode(["address", "uint256", "bool"], [checksum(pool.token), OPEN_DELTA, True]),
        encode(["address", "address", "uint256"], [ZERO_ADDRESS, checksum(recipient), OPEN_DELTA]),
    ], deadline)


def quote_calldata(pool: EthPool, buy: bool, amount_in: int) -> str:
    """V4Quoter.quoteExactInputSingle((poolKey, zeroForOne, exactAmount, hookData)) -> (amountOut, gasEstimate).
    ETH is currency0, so a buy is zeroForOne."""
    sig = f"quoteExactInputSingle(({POOL_KEY},bool,uint128,bytes))"
    return selector(sig) + encode([f"({POOL_KEY},bool,uint128,bytes)"], [(pool.key, buy, amount_in, b"")]).hex()


def decode_quote(hexdata: str) -> int:
    return int(decode(["uint256", "uint256"], bytes.fromhex(hexdata.removeprefix("0x")))[0])
