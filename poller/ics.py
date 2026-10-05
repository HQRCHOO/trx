"""Write calendar.ics (Pokémon GO events + card releases) for phone-calendar subscription.

Leek Duck times without a zone are local wall-clock times, so they're written as
"floating" times: your phone shows them at that local time wherever you are.
"""
import hashlib
import os

from .timefmt import day

GAMES = [("tcg_releases", "Pokémon TCG", "date"), ("onepiece_releases", "One Piece", "release_date"),
         ("dragonball_releases", "Dragon Ball", "release_date"), ("gundam_releases", "Gundam", "release_date")]


def _esc(s):
    return str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _dt(s):
    """ISO event time -> ICS value ('20260926T100000' floating, or '...Z' for UTC)."""
    s = str(s)
    digits = s[:19].replace("-", "").replace(":", "")
    return digits + ("Z" if s.endswith("Z") else "")


def _fold(line):
    out, b = [], line.encode("utf-8")
    while len(b) > 74:
        cut = 74
        while (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b" " + b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


def build(d, cfg):
    cal = cfg.get("calendar") or {}
    types = set(cal.get("event_types") or [])
    rules = d.get("link_rules") or {}
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//TRX//poller//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:TRX", "X-WR-CALDESC:Pokémon GO events and card releases"]
    for e in d.get("pogo_events", []):
        if types and e.get("type") not in types or not e.get("start") or not e.get("end"):
            continue
        url = (rules.get("pogo_events") or "").replace("{id}", e.get("id", ""))
        lines += ["BEGIN:VEVENT", f"UID:pogo-{e['id']}@trx", f"DTSTART:{_dt(e['start'])}", f"DTEND:{_dt(e['end'])}",
                  f"SUMMARY:{_esc(e.get('name'))}", f"DESCRIPTION:{_esc(url)}", "TRANSP:TRANSPARENT", "END:VEVENT"]
    if cal.get("card_releases", True):
        for key, label, field in GAMES:
            for r in d.get(key, []):
                dd = day(r.get(field))
                if not dd:
                    continue
                tag = "" if r.get("confirmed", True) else " (date unconfirmed)"
                lines += ["BEGIN:VEVENT", f"UID:{key}-{r.get('id')}@trx", f"DTSTART;VALUE=DATE:{dd:%Y%m%d}",
                          f"SUMMARY:{_esc(label + ': ' + str(r.get('name')) + tag)}", "TRANSP:TRANSPARENT", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


def write(d, cfg, root):
    """Write calendar.ics only when its content changed. Returns (changed, event_count)."""
    text = build(d, cfg)
    path = os.path.join(root, "calendar.ics")
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    changed = hashlib.sha256(old.encode()).digest() != hashlib.sha256(text.encode()).digest()
    if changed:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
    return changed, text.count("BEGIN:VEVENT")
