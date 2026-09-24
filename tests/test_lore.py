"""A coin's lore: the description and links its creator stored onchain, and a safe look at those links."""
import httpx

from ct_agent.sources import lore
from ct_agent.sources.lore import site_summary, split_links


def test_split_links():
    got = split_links("https://x.com/UseArclite https://arclite.app, https://t.me/arclite")
    assert got == {"x": "UseArclite", "website": "https://arclite.app", "other": ["https://t.me/arclite"]}
    assert split_links("https://twitter.com/@themutualfun/")["x"] == "themutualfun"
    assert split_links("https://x.com/i/communities/123")["x"] is None  # a community, not a profile
    assert split_links("") == {"x": None, "website": None, "other": []}


def test_website_look_stays_on_the_public_internet(monkeypatch):
    assert lore._public_host("127.0.0.1") is False and lore._public_host("10.0.0.5") is False
    assert lore._public_host("169.254.169.254") is False  # cloud metadata address
    monkeypatch.setattr(lore, "_public_host", lambda h: h == "coin.example")
    page = ('<html><head><title>ArcLite | Private batch market</title>'
            '<meta name="description" content="Orders seal before prices are read."></head>'
            '<body><script>evil()</script><h1>Trade tokenized stocks</h1></body></html>')

    def handler(req):
        if req.url.path == "/go":  # a redirect towards an internal address is refused
            return httpx.Response(302, headers={"location": "http://10.0.0.5/admin"})
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    s = site_summary(http, "https://coin.example/")
    assert s["title"] == "ArcLite | Private batch market" and s["description"] == "Orders seal before prices are read."
    assert "evil" not in s["text"] and "Trade tokenized stocks" in s["text"]
    assert site_summary(http, "https://coin.example/go") is None
    assert site_summary(http, "http://localhost/") is None and site_summary(http, "file:///etc/passwd") is None
