"""Read-only check of the launchpad's creator-fee split against pump.fun on mainnet. Sends nothing, signs nothing.

It finds live pump.fun coins still on their curve with no fee split yet, builds the exact transaction VIGIL's
launcher would send for one of its own coins (sweep_creator_fee, create_fee_sharing_config, update_fee_shares_v2 at
90/10) with that coin's real creator in the launcher's place, and asks the RPC to *simulate* it with signature checks
off. It then decodes the simulated fee-sharing account: it should be active, locked (admin revoked) and 90/10.

Run from the repo root:  .venv/Scripts/python.exe scripts/check_fee_split.py [how-many-coins]
Uses CT_SOLANA_RPC_URL if set (the public RPC rate-limits, but works for a handful of coins).
"""
from __future__ import annotations

import base64
import os
import sys
import time
from pathlib import Path

import httpx
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ct_agent.launchpad import pumpfun as pf  # noqa: E402

RPC = os.environ.get("CT_SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
REQUESTER = Pubkey.new_unique()  # stands in for the requester's wallet; any address works in a simulation
http = httpx.Client(timeout=30)


def rpc(method: str, params: list) -> dict:
    for i in range(6):
        r = http.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if r.status_code == 429:
            time.sleep(2 + 2 * i)
            continue
        j = r.json()
        if "error" in j:
            raise RuntimeError(j["error"])
        return j["result"]
    raise RuntimeError("the RPC kept rate-limiting; set CT_SOLANA_RPC_URL")


def live_coins(want: int) -> list[tuple[str, str]]:
    """(mint, creator) for coins in recent pump.fun transactions: on a SOL curve, no fee split, creator can pay rent."""
    seen, out = set(), []
    for s in rpc("getSignaturesForAddress", [str(pf.PUMP_PROGRAM), {"limit": 200}]):
        if s.get("err"):
            continue
        try:
            tx = rpc("getTransaction", [s["signature"], {"maxSupportedTransactionVersion": 1, "encoding": "json"}])
        except RuntimeError:
            continue
        for b in ((tx or {}).get("meta") or {}).get("postTokenBalances") or []:
            mint = b.get("mint")
            if not mint or mint in seen or mint == str(pf.WSOL_MINT):
                continue
            seen.add(mint)
            acc = rpc("getAccountInfo", [str(pf.bonding_curve_pda(mint)), {"encoding": "base64"}])["value"]
            try:
                bc = pf.parse_bonding_curve(base64.b64decode(acc["data"][0])) if acc else None
            except pf.LaunchBlocked:
                bc = None
            if not bc or bc["complete"] or bc["quote_mint"] not in (None, str(pf.WSOL_MINT)):
                continue
            if bc["creator"] == str(pf.sharing_config_pda(mint)):
                continue
            if rpc("getBalance", [bc["creator"]])["value"] < 20_000_000:
                continue
            out.append((mint, bc["creator"]))
            if len(out) >= want:
                return out
        time.sleep(.3)
    return out


def simulate(mint: str, creator: str) -> dict:
    me, m = Pubkey.from_string(creator), Pubkey.from_string(mint)
    shares = pf.split_for(str(REQUESTER), creator, 9_000)
    ixs = [set_compute_unit_limit(pf.FEE_TX_COMPUTE_UNITS), set_compute_unit_price(pf.FEE_TX_MICROLAMPORTS),
           pf.sweep_creator_fee_ix(me, m, me), pf.create_fee_sharing_config_ix(me, m),
           pf.update_fee_shares_v2_ix(me, m, [me], shares)]
    bh = Hash.from_string(rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])
    tx = VersionedTransaction.populate(MessageV0.try_compile(me, ixs, [], bh), [Signature.default()])
    cfg = str(pf.sharing_config_pda(mint))
    v = rpc("simulateTransaction", [base64.b64encode(bytes(tx)).decode(), {
        "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "confirmed",
        "accounts": {"encoding": "base64", "addresses": [cfg]}}])["value"]
    acc = (v.get("accounts") or [None])[0]
    after = pf.parse_sharing_config(base64.b64decode(acc["data"][0])) if acc and not v.get("err") else None
    return {"bytes": len(bytes(tx)), "units": v.get("unitsConsumed"), "err": v.get("err"), "after": after,
            "owner_ok": bool(acc) and acc.get("owner") == str(pf.PUMP_FEES_PROGRAM), "creator": creator,
            "logs": [line for line in v.get("logs") or [] if "Error" in line or "failed" in line][-3:]}


def main() -> int:
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    coins = live_coins(want)
    if not coins:
        print("no suitable live coins found right now; try again in a minute")
        return 1
    good = 0
    for mint, creator in coins:
        r = simulate(mint, creator)
        a = r["after"]
        ok = (not r["err"] and r["owner_ok"] and a and a["active"] and a["admin_revoked"]
              and [(s.address, s.bps) for s in a["shareholders"]] == [(str(REQUESTER), 9000), (creator, 1000)])
        good += bool(ok)
        print(f"{'OK  ' if ok else 'FAIL'} {mint}  {r['bytes']} bytes, {r['units']} compute units"
              + ("" if ok else f"  {r['err']} {' | '.join(r['logs'])}"))
        time.sleep(1)
    print(f"{good} of {len(coins)} simulated splits came out active, locked and 90/10")
    print("(error 6083 HolderRewardCreatorImmutable is a special coin type VIGIL never launches)")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
