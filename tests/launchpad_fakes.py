"""Offline fakes for the launchpad that speak the real wire formats: a Solana JSON-RPC node (balances, accounts,
simulate, send with signature checks, statuses), PumpPortal's trade-local (a real unsigned VersionedTransaction),
Pinata's v3 upload and X's image CDN. The node applies the pump.fun effects the launcher relies on: a create makes the
bonding curve, create_fee_sharing_config/update_fee_shares_v2 write the sharing config."""
from __future__ import annotations

import base64
import json
import struct
from typing import Any

import httpx
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from ct_agent.chain.solana import SolanaRpc
from ct_agent.launchpad import pumpfun as pf

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
BLOCKHASH = str(Hash.new_unique())
DISC_PUMP_CREATE = bytes([24, 30, 200, 40, 5, 28, 7, 119])   # pump `create`, as PumpPortal's tx would call it
CREATE_COST = 7_300_000                                       # what our fake create takes from the launcher


def curve_bytes(creator: Pubkey, complete: bool = False, creator_fee: int = 0) -> bytes:
    return (pf.DISC_BONDING_CURVE + struct.pack("<5Q", 1, 2, 3, 4, 5) + bytes([complete]) + bytes(creator)
            + b"\0\0" + bytes(32) + struct.pack("<Q", 95) + b"\0\0" + struct.pack("<QQ", creator_fee, 0) + b"\0"
            + struct.pack("<3Q", 0, 0, 0))


def config_bytes(mint: Pubkey, admin: Pubkey, shares: list[tuple[Pubkey, int]], revoked: bool = False) -> bytes:
    return (pf.DISC_SHARING_CONFIG + bytes([254, 2, 1]) + bytes(mint) + bytes(admin) + bytes([revoked])
            + struct.pack("<I", len(shares)) + b"".join(bytes(a) + struct.pack("<H", b) for a, b in shares))


class FakeSolana:
    """JSON-RPC over an httpx.MockTransport. `rpc` is a real SolanaRpc pointed at it."""

    def __init__(self) -> None:
        self.balances: dict[str, int] = {}
        self.accounts: dict[str, tuple[str, bytes]] = {}   # address -> (owner, data)
        self.statuses: dict[str, dict[str, Any] | None] = {}
        self.sent: list[VersionedTransaction] = []
        self.simulated: list[VersionedTransaction] = []
        self.sim_err: Any = None
        self.land = True          # sent transactions confirm; False: the node never hears of them
        self.fail_on_chain: Any = None
        self.costs: dict[str, int] = {}
        self.rpc = SolanaRpc("https://rpc.test", transport=httpx.MockTransport(self._handle), retries=0)

    # --- state helpers ---
    def set_account(self, addr: Pubkey | str, owner: Pubkey, data: bytes) -> None:
        self.accounts[str(addr)] = (str(owner), data)

    def _info(self, addr: str) -> dict[str, Any] | None:
        if addr not in self.accounts:
            return None
        owner, data = self.accounts[addr]
        return {"owner": owner, "lamports": self.balances.get(addr, 1_000_000), "executable": False,
                "data": [base64.b64encode(data).decode(), "base64"]}

    def _apply(self, tx: VersionedTransaction) -> None:
        """What the chain would do with the instructions the launcher sends."""
        msg = tx.message
        keys = list(msg.account_keys)
        payer = keys[0]
        for ix in msg.instructions:
            prog, data = keys[ix.program_id_index], bytes(ix.data)
            acc = [keys[i] for i in bytes(ix.accounts)]
            if prog == pf.PUMP_PROGRAM and data[:8] == DISC_PUMP_CREATE:
                mint = acc[0]
                self.set_account(pf.bonding_curve_pda(mint), pf.PUMP_PROGRAM, curve_bytes(payer))
                self.balances[str(payer)] = self.balances.get(str(payer), 0) - CREATE_COST
                self.costs[str(tx.signatures[0])] = CREATE_COST
            elif prog == pf.PUMP_FEES_PROGRAM and data[:8] == pf.DISC_CREATE_FEE_SHARING_CONFIG:
                mint = acc[4]
                cfg = pf.sharing_config_pda(mint)
                self.set_account(cfg, pf.PUMP_FEES_PROGRAM, config_bytes(mint, payer, [(payer, 10_000)]))
                self.set_account(pf.bonding_curve_pda(mint), pf.PUMP_PROGRAM, curve_bytes(cfg))
            elif prog == pf.PUMP_FEES_PROGRAM and data[:8] == pf.DISC_UPDATE_FEE_SHARES_V2:
                mint = acc[4]
                n = struct.unpack_from("<I", data, 8)[0]
                shares = [(Pubkey.from_bytes(data[12 + 34 * i:44 + 34 * i]), struct.unpack_from("<H", data, 44 + 34 * i)[0])
                          for i in range(n)]
                self.set_account(pf.sharing_config_pda(mint), pf.PUMP_FEES_PROGRAM,
                                 config_bytes(mint, payer, shares, revoked=True))
            elif prog == pf.PUMP_PROGRAM and data[:8] == pf.DISC_DISTRIBUTE_CREATOR_FEES_V2:
                vault = str(acc[4])
                self.balances[vault] = pf.RENT_EXEMPT_EMPTY

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body["params"]
        result: Any
        if method == "getBalance":
            result = {"context": {"slot": 1}, "value": self.balances.get(params[0], 0)}
        elif method == "getAccountInfo":
            result = {"context": {"slot": 1}, "value": self._info(params[0])}
        elif method == "getLatestBlockhash":
            result = {"context": {"slot": 1}, "value": {"blockhash": BLOCKHASH, "lastValidBlockHeight": 100}}
        elif method == "simulateTransaction":
            tx = VersionedTransaction.from_bytes(base64.b64decode(params[0]))
            self.simulated.append(tx)
            value: dict[str, Any] = {"err": self.sim_err, "logs": ["Program log: fake"], "accounts": None}
            watch = (params[1].get("accounts") or {}).get("addresses")
            if watch:
                payer = str(tx.message.account_keys[0])
                value["accounts"] = [{"lamports": self.balances.get(a, 0) - (CREATE_COST if a == payer else 0)}
                                     for a in watch]
            result = {"context": {"slot": 1}, "value": value}
        elif method == "sendTransaction":
            tx = VersionedTransaction.from_bytes(base64.b64decode(params[0]))
            assert all(tx.verify_with_results()), "a signature doesn't match its signer"
            sig = str(tx.signatures[0])
            self.sent.append(tx)
            if self.land:
                if self.fail_on_chain:
                    self.statuses[sig] = {"confirmationStatus": "confirmed", "err": self.fail_on_chain}
                else:
                    self._apply(tx)
                    self.statuses[sig] = {"confirmationStatus": "confirmed", "err": None}
            result = sig
        elif method == "getSignatureStatuses":
            result = {"context": {"slot": 1}, "value": [self.statuses.get(s) for s in params[0]]}
        elif method == "getTransaction":
            c = self.costs.get(params[0])
            result = None if c is None else {"meta": {"preBalances": [10**9], "postBalances": [10**9 - c]}}
        else:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "error": {"code": -32601, "message": f"no {method}"}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


class FakeWeb:
    """PumpPortal trade-local, Pinata v3 uploads and X's image CDN on one MockTransport."""

    def __init__(self) -> None:
        self.portal_requests: list[dict[str, Any]] = []
        self.pins: list[dict[str, Any]] = []
        self.portal_status = 200
        self.extra_signer: Keypair | None = None   # make PumpPortal's tx want a stranger's signature
        self.extra_program: Pubkey | None = None   # or call a program a create never calls
        self.transport = httpx.MockTransport(self._handle)

    def unsigned_create(self, payer: Pubkey, mint: Pubkey) -> bytes:
        accounts = [AccountMeta(mint, True, True), AccountMeta(payer, True, True),
                    AccountMeta(pf.bonding_curve_pda(mint), False, True), AccountMeta(pf.SYSTEM_PROGRAM, False, False)]
        if self.extra_signer:
            accounts.append(AccountMeta(self.extra_signer.pubkey(), True, False))
        ixs = [Instruction(pf.PUMP_PROGRAM, DISC_PUMP_CREATE + b"args", accounts)]
        if self.extra_program:
            ixs.append(Instruction(self.extra_program, b"\1", [AccountMeta(payer, False, True)]))
        msg = MessageV0.try_compile(payer, ixs, [], Hash.from_string(BLOCKHASH))
        unsigned = VersionedTransaction.populate(msg, [Signature.default()] * msg.header.num_required_signatures)
        return bytes(unsigned)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://pumpportal.fun/api/trade-local"):
            body = json.loads(request.content)
            self.portal_requests.append(body)
            if self.portal_status != 200:
                return httpx.Response(self.portal_status, text="Bad Request")
            return httpx.Response(200, content=self.unsigned_create(Pubkey.from_string(body["publicKey"]),
                                                                   Pubkey.from_string(body["mint"])))
        if url == pf.PINATA_UPLOAD_URL:
            assert request.headers["authorization"] == "Bearer test-jwt"
            raw = request.content
            name = "metadata" if b'filename="' in raw and b".json" in raw else "image"
            entry: dict[str, Any] = {"kind": name, "public": b'name="network"\r\n\r\npublic' in raw}
            if name == "metadata":
                start = raw.index(b"{")
                entry["json"] = json.loads(raw[start:raw.rindex(b"}") + 1])
            self.pins.append(entry)
            cid = f"bafy{name}{len(self.pins)}"
            return httpx.Response(200, json={"data": {"id": "x", "cid": cid, "name": name}})
        if url.startswith("https://pbs.twimg.com/"):
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404)
