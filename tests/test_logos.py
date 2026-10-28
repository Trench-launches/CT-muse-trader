import io

import httpx
from PIL import Image

from ct_agent.api.logos import _candidates, logo_file

TOKEN = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (400, 400), (0, 130, 251, 255)).save(out, "PNG")
    return out.getvalue()


def _db_with_logo(db, url):
    db.execute("INSERT INTO tokens(mint,decimals,first_seen,logo_url) VALUES(?,?,?,?)", (TOKEN, 5, "now", url))


def test_only_ipfs_content_ids_are_fetched_through_our_gateways():
    assert _candidates("ipfs://bafkreiabc")[0] == "https://ipfs.io/ipfs/bafkreiabc"
    assert _candidates("https://attacker.example/ipfs/bafy1")[0] == "https://ipfs.io/ipfs/bafy1"
    assert _candidates("https://attacker.example/logo.png") == [] and _candidates("http://10.0.0.1/ipfs/x") == []
    assert _candidates("https://dd.dexscreener.com/ds-data/tokens/solana/x.png") ==         ["https://dd.dexscreener.com/ds-data/tokens/solana/x.png"]  # DexScreener's own CDN, as given
    assert _candidates("https://dd.dexscreener.com:8443/x.png") == []


def test_logo_is_reencoded_small_and_cached(db, tmp_path):
    _db_with_logo(db, "ipfs://bafkreilogo")
    calls = []

    def h(req):
        calls.append(str(req.url))
        return httpx.Response(200, content=_png())
    path = logo_file(db, tmp_path, TOKEN, client=httpx.Client(transport=httpx.MockTransport(h)))
    im = Image.open(path)
    assert im.format == "WEBP" and max(im.size) == 128 and calls == ["https://ipfs.io/ipfs/bafkreilogo"]
    assert logo_file(db, tmp_path, TOKEN, client=httpx.Client(transport=httpx.MockTransport(h))) == path
    assert len(calls) == 1  # served from disk the second time


def test_non_images_are_rejected(db, tmp_path):
    _db_with_logo(db, "ipfs://bafkreijunk")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<svg onload=alert(1)>")))
    assert logo_file(db, tmp_path, TOKEN, client=client) is None
