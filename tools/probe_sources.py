"""Diagnostic: can candidate Spanish stores/feeds be read legitimately from THIS machine/IP?

For each candidate: homepage status (anti-bot?), robots.txt rules for '*', the site's own schema.org
SearchAction (or a fallback search URL), search page status, and how much structured product data it exposes.
Read-only, one request per URL, no evasion. Usage: python tools/probe_sources.py [name ...]
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.robotparser
from urllib.parse import quote, urlsplit

import httpx

UA = "Mozilla/5.0 (compatible; ps5-slim-monitor/1.0; personal price alert)"
Q = "ps5 slim"

CANDIDATES: dict[str, dict[str, str]] = {
    "playstation_direct": {"home": "https://direct.playstation.com/es-es/",
                           "search": "https://direct.playstation.com/es-es/buy-consoles"},
    "mediamarkt": {"home": "https://www.mediamarkt.es/", "search": "https://www.mediamarkt.es/es/search.html?query={q}"},
    "game": {"home": "https://www.game.es/", "search": "https://www.game.es/buscar/{q}"},
    "elcorteingles": {"home": "https://www.elcorteingles.es/", "search": "https://www.elcorteingles.es/search-nwx/1/?s={q}"},
    "hipercor": {"home": "https://www.hipercor.es/", "search": "https://www.hipercor.es/search/?s={q}"},
    "carrefour": {"home": "https://www.carrefour.es/", "search": "https://www.carrefour.es/?q={q}"},
    "pccomponentes": {"home": "https://www.pccomponentes.com/", "search": "https://www.pccomponentes.com/buscar/?query={q}"},
    "fnac": {"home": "https://www.fnac.es/", "search": "https://www.fnac.es/SearchResult/ResultList.aspx?Search={q}"},
    "worten": {"home": "https://www.worten.es/", "search": "https://www.worten.es/search?query={q}"},
    "xtralife": {"home": "https://www.xtralife.com/", "search": "https://www.xtralife.com/buscar/{q}"},
    "alcampo": {"home": "https://www.compraonline.alcampo.es/", "search": "https://www.compraonline.alcampo.es/search?q={q}"},
    "toysrus": {"home": "https://www.toysrus.es/", "search": "https://www.toysrus.es/search?q={q}"},
    "tien21": {"home": "https://www.tien21.es/", "search": "https://www.tien21.es/catalogsearch/result/?q={q}"},
    "mielectro": {"home": "https://www.mielectro.es/", "search": "https://www.mielectro.es/buscador/?q={q}"},
    "electrocosto": {"home": "https://www.electrocosto.com/", "search": "https://www.electrocosto.com/buscar?q={q}"},
    "lidl": {"home": "https://www.lidl.es/", "search": "https://www.lidl.es/q/search?q={q}"},
    "eroski": {"home": "https://www.eroski.es/", "search": "https://www.eroski.es/"},
    "euronics": {"home": "https://www.euronics.es/", "search": "https://www.euronics.es/catalogsearch/result/?q={q}"},
    "milar": {"home": "https://www.milar.es/", "search": "https://www.milar.es/catalogsearch/result/?q={q}"},
    "coolmod": {"home": "https://www.coolmod.com/", "search": "https://www.coolmod.com/"},
    "amazon": {"home": "https://www.amazon.es/", "search": "https://www.amazon.es/"},
    # Deal feeds / public channels
    "blogdechollos_rss": {"home": "https://www.blogdechollos.com/feed/", "search": ""},
    "michollo_rss": {"home": "https://www.michollo.com/feed/", "search": ""},
    "chollometro_rss": {"home": "https://www.chollometro.com/rss/nuevos", "search": ""},
    "game_telegram": {"home": "https://t.me/s/videojuegosGAMEofertasoficial", "search": ""},
}


def robots_for(client: httpx.Client, home: str) -> urllib.robotparser.RobotFileParser | None:
    parts = urlsplit(home)
    try:
        r = client.get(f"{parts.scheme}://{parts.netloc}/robots.txt")
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp


def summarize(html: str) -> str:
    blocks = re.findall(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I)
    types: list[str] = []
    for b in blocks:
        try:
            types += re.findall(r'"@type"\s*:\s*"(Product|ItemList|Offer|SearchAction)"', json.dumps(json.loads(b)))
        except json.JSONDecodeError:
            pass
    links = set(re.findall(r'href="([^"]*(?:ps5|playstation-5|playstation5)[^"]*)"', html, re.I))
    challenge = "just a moment" in html[:5000].lower() or "captcha" in html[:5000].lower()
    return (f"ld={sorted(set(types))} n_product={types.count('Product')} ps5_links={len(links)}"
            + (" CHALLENGE" if challenge else ""))


def main(names: list[str]) -> None:
    client = httpx.Client(headers={"User-Agent": UA, "Accept-Language": "es-ES,es;q=0.9"}, timeout=20,
                          follow_redirects=True)
    for name, c in CANDIDATES.items():
        if names and name not in names:
            continue
        out = [f"{name:20}"]
        try:
            r = client.get(c["home"])
            out.append(f"home={r.status_code} {summarize(r.text) if r.status_code == 200 else ''}")
            tmpl = re.search(r'"urlTemplate"\s*:\s*"([^"]+)"', r.text)
            search = c["search"]
            if tmpl:
                out.append(f"searchAction={tmpl.group(1)[:90]}")
            if search:
                url = search.replace("{q}", quote(Q))
                rp = robots_for(client, c["home"])
                allowed = rp.can_fetch("*", url) if rp else "no-robots"
                time.sleep(1)
                s = client.get(url)
                out.append(f"robots_allows_search={allowed} search={s.status_code} -> {s.url} "
                           f"{summarize(s.text) if s.status_code == 200 else ''}")
        except httpx.HTTPError as exc:
            out.append(f"ERROR {type(exc).__name__}")
        print(" | ".join(out), flush=True)
        time.sleep(1)


if __name__ == "__main__":
    main(sys.argv[1:])
