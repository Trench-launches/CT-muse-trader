"""Stocks a launchpad coin can be paired with instead of ETH: the Robinhood stock and ETF tokens Pons approves
as pair tokens (PonsV2LaunchFactory.approvedPairTokens, read from its PairTokenApprovalUpdated log on 2026-09-25).

A stock-paired coin is bought and sold with that stock token, and its creator fees are paid in it. The factory
checks approval again at launch, so a pair Pons later drops fails the simulation instead of launching.
"""
from __future__ import annotations

import re

# symbol -> (display name, token address on Robinhood Chain); all 18 decimals
STOCK_PAIRS: dict[str, tuple[str, str]] = {
    "AAPL": ("Apple", "0xaF3D76f1834A1d425780943C99Ea8A608f8a93f9"),
    "AMC": ("AMC", "0x05a3d1Cd21d0C88145E82600E62e7E496e0F222B"),
    "AMD": ("AMD", "0x86923f96303D656E4aa86D9d42D1e57ad2023fdC"),
    "AMZN": ("Amazon", "0x12f190a9F9d7D37a250758b26824B97CE941bF54"),
    "BA": ("Boeing", "0x4D21483a44Bf67a86b77E3dA301411880797D452"),
    "BABA": ("Alibaba", "0xad25Ac6C84D497db898fa1E8387bf6Af3532a1c4"),
    "BB": ("BlackBerry", "0x48E39E56aCdbA37b09020C0b734A613C9a2f100A"),
    "BE": ("Bloom Energy", "0x822CC93fFD030293E9842c30BBD678F530701867"),
    "BULL": ("Webull", "0xceF9027c7d6985b85f0BA431125073529A947A68"),
    "CEG": ("Constellation Energy", "0xaE517A2903E68bd929Dfd15be875F8369D53e94a"),
    "COIN": ("Coinbase", "0x6330D8C3178a418788dF01a47479c0ce7CCF450b"),
    "COST": ("Costco", "0x4EA005168D7F09a7A0Ba9D1DEf21a479950E44C2"),
    "CRCL": ("Circle", "0xdF0992E440dD0be65BD8439b609d6D4366bf1CB5"),
    "DELL": ("Dell", "0x941AE714EC6D8130c7B75d67160Ca08f1e7d11Dd"),
    "DJT": ("Trump Media", "0x1D11f0496982706C5e14A514D4E79F2e6BdE4516"),
    "EWY": ("South Korea ETF", "0x7f0aBeF0C07280F82c6a08ead09dEd6BAE2C13Fc"),
    "F": ("Ford", "0x25C288E6D899b9BC30160965aD9644c67e73bE0C"),
    "FIG": ("Figma", "0x41F4267525a8AFf329540eF24fD83d9044758B33"),
    "GLD": ("Gold ETF", "0xC9a981FEE1F9DEc688bb123ccDeCc63D0deBFC4e"),
    "GME": ("GameStop", "0x1b0E319c6A659F002271B69dB8A7df2F911c153E"),
    "GOOGL": ("Alphabet", "0x2e0847E8910a9732eB3fb1bb4b70a580ADAD4FE3"),
    "HIMS": ("Hims & Hers", "0xCceE82fE024c36fA15E1005edE3E9e4787e23D09"),
    "IBM": ("IBM", "0x980dcf6766FA79f5Cf0c4AAdb3ab477ff15a9619"),
    "INDA": ("India ETF", "0xACEF2e09adb47aD6aBeBAD9fF06689E60615C2B6"),
    "INTC": ("Intel", "0xc72b96e0E48ecd4DC75E1e45396e26300BC39681"),
    "JNJ": ("Johnson & Johnson", "0x03DfbBE0AC4E7bCDaFd08eD41A400326B77D8c80"),
    "LLY": ("Eli Lilly", "0x8005d266423c7ea827372c9c864491e5786600ea"),
    "LMT": ("Lockheed Martin", "0x329fcACEb9AD6F9580DD5F643fed0646900D043c"),
    "LULU": ("Lululemon", "0x4e62068525Ab11FE768e29dfD00ef909B9803016"),
    "META": ("Meta", "0xc0D6457C16Cc70d6790Dd43521C899C87ce02f35"),
    "MRNA": ("Moderna", "0x43B07D15cE533bEc5476d70C22a78a1B2B662155"),
    "MRVL": ("Marvell", "0x62fd0668e10D8B72339BE2DCF7643001688ff13B"),
    "MSFT": ("Microsoft", "0xe93237C50D904957Cf27E7B1133b510C669c2e74"),
    "MSTR": ("Strategy", "0xec262a75e413fAfD0dF80480274532C79D42da09"),
    "MU": ("Micron", "0xfF080c8ce2E5feadaCa0Da81314Ae59D232d4afD"),
    "NET": ("Cloudflare", "0x116F00968269B7bfbaD4109cE591d6E74c0601d4"),
    "NFLX": ("Netflix", "0xE0444EF8BF4eD74f74FD73686e2ddF4C1c5591E8"),
    "NU": ("Nu Holdings", "0x408c14038a04f7bD235329E26d2bf569ee20e250"),
    "NVDA": ("NVIDIA", "0xd0601CE157Db5bdC3162BbaC2a2C8aF5320D9EEC"),
    "PFE": ("Pfizer", "0x7066A64c24e4206CD62E83bf198c1E7EB361F51e"),
    "PLTR": ("Palantir", "0x894E1EC2D74FFE5AEF8Dc8A9e84686acCB964F2A"),
    "QQQ": ("Nasdaq-100 ETF", "0xD5f3879160bc7c32ebb4dC785F8a4F505888de68"),
    "QUBT": ("Quantum Computing", "0x59818904ab4cE163b3cE4FfB64f2D6Ca02c434B4"),
    "RBLX": ("Roblox", "0xF0C4BF4C582cb3836e98394b1d4e7B7281101bE8"),
    "RCAT": ("Red Cat", "0xFDE6b5d9BB419B10C23268c74e369AbFF39C0460"),
    "RDDT": ("Reddit", "0x05b37Fb53A299a1b874A619e1c4C404D52C36F4C"),
    "RIVN": ("Rivian", "0xB1BF26c1D20ff267A4f93550d1E0d06ac40a114B"),
    "RKLB": ("Rocket Lab", "0x3b14C39E89D60D627b42a1A4CA45b5bb45Fc12e2"),
    "SGOV": ("Treasury bond ETF", "0x92FD66527192E3e61d4DDd13322Aa222DE86F9B5"),
    "SHOP": ("Shopify", "0xF53F66751B1Eff985311b693531E3290F600c410"),
    "SKHY": ("SK hynix", "0x84CAb63bc87912E71ad199ff14A0bA45de68FeF8"),
    "SLV": ("Silver ETF", "0x411eFb0E7f985935DAec3D4C3ebaEa0d0AD7D89f"),
    "SNAP": ("Snap", "0xF6589F11Bc40b669e584073F428B05562F568733"),
    "SNDK": ("Sandisk", "0xB90A19fF0Af67f7779afF50A882A9CfF42446400"),
    "SNOW": ("Snowflake", "0xBa0CAB75495255d0cB58E22B648bFED4ECD1F47E"),
    "SPCX": ("SpaceX", "0x4a0E65A3EcceC6dBe60AE065F2e7bb85Fae35eEa"),
    "SPY": ("S&P 500 ETF", "0x117cc2133c37B721F49dE2A7a74833232B3B4C0C"),
    "TSLA": ("Tesla", "0x322F0929c4625eD5bAd873c95208D54E1c003b2d"),
    "TSM": ("TSMC", "0x58FfE4a942d3885bAa22D7520691F611EF09e7AA"),
    "TTWO": ("Take-Two", "0x5e81213613b6B86EaB4c6c50d718d34359459786"),
    "UPS": ("UPS", "0xf23250dac154D05Bb671CB0d0eBEf3c635c79CE2"),
    "USO": ("Oil ETF", "0xa30FA36Db767ad9eD3f7a60fC79526fB4d56D344"),
    "WYFI": ("WhiteFiber", "0x9e7ABD3C9139D14E4c86DcE0e455AAB7A0C2FB3E"),
}

# "pair: TSLA", "pair = $nvda", "paired with $TSLA", "pair it with AAPL", "pair $GME" (not "a pair of socks")
_PAIR = re.compile(r"\bpair(?:ed)?\s*(?::|=|\s(?:it\s)?with\b)\s*\$?([A-Za-z]{1,6})\b|\bpair(?:ed)?\s+\$([A-Za-z]{1,6})\b", re.I)


def asked_pair(text: str) -> str | None:
    """The pair the post asks for, upper-cased as written ("ETH" included), or None if it doesn't ask for one."""
    m = _PAIR.search(text or "")
    return (m.group(1) or m.group(2)).upper() if m else None


def pair_token(symbol: str) -> str | None:
    hit = STOCK_PAIRS.get(symbol.upper())
    return hit[1] if hit else None
