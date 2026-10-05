"""Pokémon Center "queue is live" watch (v3.62).

Pokémon Center blocks automated visitors and runs a virtual queue during drops, so TRX never reads
pokemoncenter.com. Instead it watches public chatter: the newest posts on r/PKMNTCGDeals and PokeBeach's
news feed. "Reported live" = 2+ matching posts in the last 45 minutes, or 1 post with an exact
"queue is live / queue is up" phrase. Secondhand by nature; the Slack ping says "verify".
"""
import datetime as dt
import html as htmlmod
import re
import xml.etree.ElementTree as ET

from ..util import get, result

FEEDS = (("r/PKMNTCGDeals", "https://www.reddit.com/r/PKMNTCGDeals/new/.rss"),
         ("PokeBeach", "https://www.pokebeach.com/feed"))
WINDOW_MIN = 45
PC = re.compile(r"pok[eé]mon\s*center|\bpoke?\s*center\b|\bPC\s+(queue|drop|restock)", re.I)
SIGNAL = re.compile(r"\bqueue\b|restock|back in stock|pre-?orders?\s+(are\s+)?(live|up|open)|\blive now\b|\bis live\b|\bdropp?(ed|ing)\b", re.I)
EXACT = re.compile(r"queue\s+(is\s+)?(live|up|open)|in\s+(the\s+)?queue\s+now", re.I)
NS = {"a": "http://www.w3.org/2005/Atom"}


def _when(s):
    for fmt in (None, "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if fmt is None else dt.datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _entries(xml_text):
    root = ET.fromstring(xml_text)
    out = []
    for e in root.findall("a:entry", NS):                      # Atom (Reddit)
        out.append((e.findtext("a:title", "", NS), e.findtext("a:content", "", NS),
                    (e.find("a:link", NS).get("href") if e.find("a:link", NS) is not None else ""),
                    e.findtext("a:published", "", NS) or e.findtext("a:updated", "", NS)))
    for it in root.iter("item"):                               # RSS 2.0 (PokeBeach)
        out.append((it.findtext("title", ""), it.findtext("description", ""), it.findtext("link", ""), it.findtext("pubDate", "")))
    return out


def fetch(cfg, prev):
    now = dt.datetime.now(dt.timezone.utc)
    posts, status = [], {}
    for name, url in FEEDS:
        try:
            txt = get(url, headers={"User-Agent": "TRX dashboard (personal; reads public feed)"}, timeout=20).text
            n = 0
            for title, body, link, when in _entries(txt):
                t = _when(when or "")
                if not t or (now - t).total_seconds() > WINDOW_MIN * 60:
                    continue
                text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", f"{title} {body}"))
                if PC.search(text) and SIGNAL.search(text):
                    posts.append({"source": name, "title": htmlmod.unescape(title)[:140], "url": link, "at": t.isoformat(),
                                  "exact": bool(EXACT.search(text))})
                    n += 1
            status[name] = f"ok ({n} matching)"
        except Exception as e:
            status[name] = f"unreadable ({type(e).__name__})"
    if all(v.startswith("unreadable") for v in status.values()):
        return result(ok=False, error="; ".join(f"{k}: {v}" for k, v in status.items()))
    live = len(posts) >= 2 or any(p["exact"] or p["source"] == "PokeBeach" for p in posts)   # a PokeBeach article counts alone
    old = prev.get("pc_queue") or {}
    since = old.get("since") if (live and old.get("live")) else (now.isoformat() if live else None)
    return result(posts, pc_queue={"live": live, "since": since, "posts": posts[:5], "checked_at": now.isoformat(),
                                   "feeds": status, "last_ping": old.get("last_ping")},
                  note=" · ".join(f"{k} {v}" for k, v in status.items()))
