import datetime as dt
"""TRX poller: fetch due sources, merge into dashboard.json, alert on changes.

Usage:
  python -m poller.main               # normal scheduled run
  python -m poller.main --force       # run every source now
  python -m poller.main --dry-run     # fetch and print, write nothing, send nothing
  python -m poller.main --test-alert  # send one Discord test message and exit
"""
import argparse
import copy
import importlib
import os
import sys

import yaml

from . import alerts, diff, drive, ics, icons, notify  # ics.py writes calendar.ics (not named calendar.py, so it can never shadow Python's built-in calendar module)
from .util import now_iso, now_utc, parse_iso, result

HEARTBEAT_HOURS = 2  # rewrite dashboard.json at least this often even if nothing changed
VOLATILE = ("generated_at", "last_run", "last_success", "checked_at", "fails", "runs_24h")  # timestamps ignored when deciding "changed"


def signature(d):
    """The dashboard with per-run timestamps removed, for change detection."""
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in VOLATILE}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node
    import json as _json
    return _json.dumps(strip(d), sort_keys=True, ensure_ascii=False)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# source name -> (cadence group, dashboard key)
SOURCES = {
    "scrapedduck": ("pogo_events", "pogo_events"),
    "sdraids": ("raids", "raids"),
    "sdresearch": ("raids", "research"),
    "sdeggs": ("raids", "eggs"),
    "sdrocket": ("raids", "rocket"),
    "pgoapi": ("raids", "raid_difficulty"),
    "sdmax": ("raids", "power_spots"),
    "pcqueue": ("pcqueue", "pc_queue_posts"),
    "gamedata": ("raids", "gamedata_unused"),
    "pokemonsets": ("card_games", "tcg_releases"),
    "gcg": ("card_games", "gundam_releases"),
    "onepiece": ("card_games", "onepiece_releases"),
    "dragonball": ("card_games", "dragonball_releases"),
    "bestbuy": ("stock", "stock"),
    "target": ("stock", "stock"),
    "gamestop": ("stock", "stock"),
    "discord": ("discord", "discord_posts"),
    "trackers": ("trackers", "stock"),
    "nintendo": ("nintendo", "stock"),
    "bestbuy_button": ("bestbuy_button", "stock"),
    "walmart": ("walmart", "stock"),
}
STOCK_SOURCES = ("bestbuy", "target", "gamestop", "trackers", "nintendo", "bestbuy_button", "walmart")
FIRST_HAND = ("bestbuy", "target", "gamestop", "nintendo", "bestbuy_button", "walmart")
LINK_RULES = {"pogo_events": "https://leekduck.com/events/{id}/",
              "gundam_releases": "https://www.gundam-gcg.com/en/products/{id}.html"}


def load_cfg():
    with open(os.path.join(ROOT, "config", "watchlist.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def due(state, group, cfg, force):
    if force:
        return True
    last = parse_iso((state or {}).get("last_run"))
    mins = float((cfg.get("cadence_minutes") or {}).get(group, 15))
    return last is None or (now_utc() - last).total_seconds() >= (mins - 1) * 60


def garbled(name):
    """A scraped name that swallowed other text (a price, or far too long)."""
    n = str(name or "")
    return "MSRP" in n.upper() or len(n) > 100


def merge_manual(scraped, manual):
    """Scraped rows win on dates/prices; manual rows fill gaps and add unlisted sets.
    A garbled scraped name is replaced by the manual name when one exists."""
    by_id = {r["id"]: dict(r) for r in scraped if r.get("id")}
    for m in manual or []:
        m = dict(m)
        m.setdefault("id", m.get("name", "item").lower().replace(" ", "-"))
        if m.get("date") and not m.get("release_date"):
            m["release_date"] = str(m.pop("date"))
        if m.get("release_date") is not None:
            m["release_date"] = str(m["release_date"])
        if m["id"] in by_id and garbled(by_id[m["id"]].get("name")) and m.get("name"):
            by_id[m["id"]]["name"] = m["name"]
            if m.get("category"):
                by_id[m["id"]]["category"] = m["category"]
        if m["id"] in by_id:
            got, want = str(by_id[m["id"]].get("release_date") or ""), str(m.get("release_date") or "")
            if len(got) == 7 and len(want) == 10 and want.startswith(got):
                by_id[m["id"]]["release_date"] = want  # page shows the month; manual has the day
            for k, v in m.items():
                if by_id[m["id"]].get(k) in (None, "") and v not in (None, ""):
                    by_id[m["id"]][k] = v
        else:
            m.setdefault("source", "manual")
            by_id[m["id"]] = m
    return list(by_id.values())


RETAILER = {"bestbuy": "Best Buy", "target": "Target", "gamestop": "GameStop", "nintendo": "Nintendo Store",
            "bestbuy_button": "Best Buy", "walmart": "Walmart"}


def placeholders(cfg, results):
    """A 'not configured' row for each watched item whose retailer isn't set up yet."""
    rows = []
    for w in cfg.get("stock", []):
        r = w.get("retailer")
        res = results.get(r)
        if res and res.get("ok") is None:
            item = w.get("item") or "Pokémon GO Plus +"
            rows.append({"id": f"{r}-setup-{item}".lower().replace(" ", "-"), "item": item,
                         "retailer": RETAILER.get(r, r), "area": None, "store": None,
                         "status": "not_configured", "price": None, "url": None,
                         "note": f"{res.get('error')}: add IDs/keys (guide §12)", "src": r})
    return rows


def runs_24h(out=print):
    """How often this workflow actually ran in the last 24 h (GitHub drops many scheduled triggers)."""
    import datetime as _dt
    import requests
    tok, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not tok or not repo:
        return None
    since = (now_utc() - _dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/poll.yml/runs",
                         params={"created": f">={since}", "per_page": 100},
                         headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}, timeout=15)
        r.raise_for_status()
        rs = r.json().get("workflow_runs") or []
    except Exception as e:
        out(f"runs: couldn't read run history ({type(e).__name__})")
        return None
    res = {"total": len(rs), "scheduled": sum(1 for x in rs if x.get("event") == "schedule"),
           "manual": sum(1 for x in rs if x.get("event") != "schedule"),
           "failed": sum(1 for x in rs if x.get("conclusion") == "failure"), "expected": 96, "as_of": now_iso()}
    out(f"runs: {res['total']} in 24 h ({res['scheduled']} scheduled, {res['manual']} manual, {res['failed']} failed) of 96 expected")
    return res


def run(only=None, force=False, dry=False, out=print):
    cfg = load_cfg()
    prev = drive.read_dashboard() or {}
    new = copy.deepcopy(prev)
    old_states = new.get("sources") or {}
    states = new["sources"] = {k: old_states[k] for k in SOURCES if k in old_states}  # drop retired names
    results = {}
    out(f"[{now_iso()}] run start · force={force} dry={dry}")

    for name, (group, key) in SOURCES.items():
        if only and name not in only:
            continue
        st = states.setdefault(name, {"ok": None, "fails": 0})
        if not due(st, group, cfg, force):
            continue
        try:
            res = importlib.import_module(f"poller.sources.{name}").fetch(cfg, prev)
        except Exception as e:  # one broken source never stops the others
            res = result(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}")
        if res["ok"] and name in ("onepiece", "dragonball"):
            man_ids = {str(x.get("id")) for x in ((cfg.get("card_games") or {}).get(name) or {}).get("manual") or []}
            res["items"] = [r for r in res["items"] if not garbled(r.get("name")) or r.get("id") in man_ids]
            if not res["items"]:
                res = result(ok=False, error="page layout changed: 0 products matched", debug=res.get("debug"))
        results[name] = res
        st["last_run"] = now_iso()
        if res["ok"]:
            st.update(ok=True, last_success=now_iso(), fails=0, error=None, count=res["count"])
        elif res["ok"] is None:
            st.update(ok=None, fails=0, error=res["error"], count=0)
        else:
            st.update(ok=False, fails=int(st.get("fails", 0)) + 1, error=res["error"])
        st.pop("note_auto", None)
        if res.get("primary_error"):
            st["note"] = f"pokemontcg.io failed ({res['primary_error']}); using {res.get('api') or 'TCGdex'}"
        elif name == "pokemonsets" and res["ok"]:
            st.pop("note", None)
        if name in ("trackers", "gamestop", "nintendo") and res.get("note"):
            st["note"] = res["note"]
        # a card reader that fails falls back to the manual list: shown as "manual", not "down"
        if name in ("onepiece", "dragonball"):
            has_manual = bool(((cfg.get("card_games") or {}).get(name) or {}).get("manual"))
            st["manual_fallback"] = bool(res["ok"] is False and has_manual)
        if res.get("debug"):
            st["debug_snippet"] = str(res["debug"])[:1500]
        elif res["ok"]:
            st.pop("debug_snippet", None)
        flag = {True: "ok  ", None: "skip", False: "FAIL"}[res["ok"]]
        out(f"{name:<14} {flag} {res.get('count', 0):>3} items   {res.get('error') or ''}".rstrip())

        if res["ok"] and name not in STOCK_SOURCES and name not in ("onepiece", "dragonball", "pokemonsets"):
            new[key] = res["items"]
        if name == "pokemonsets" and res.get("tcgdex_cache"):
            new["tcgdex_cache"] = res["tcgdex_cache"]
        if name == "pcqueue" and res.get("ok") and res.get("pc_queue"):
            new["pc_queue"] = res["pc_queue"]
        if name == "sdmax" and res.get("ok") and res.get("roster") is not None:
            new["max_roster"] = res["roster"]
        if name == "gamedata" and res.get("ok"):
            new["gamedata"] = {k: res.get(k) for k in ("counters", "type_top", "dex", "cpm", "rocket_teams")}
        if name in ("gamedata", "onepiece", "pokemonsets", "sdmax", "pcqueue") and res.get("note"):
            st["note"] = res["note"]

    new.pop("gamedata_unused", None)
    new.pop("pc_queue_posts", None)
    # Card games: scraped (if it worked) + manual lists from the watchlist.
    games = cfg.get("card_games") or {}
    for name, key, gkey in (("onepiece", "onepiece_releases", "onepiece"), ("dragonball", "dragonball_releases", "dragonball")):
        res = results.get(name)
        base = res["items"] if res and res["ok"] else prev.get(key, [])
        new[key] = merge_manual(base, (games.get(gkey) or {}).get("manual"))
    manual_pk = [dict(r, id=r.get("id") or r["name"].lower().replace(" ", "-").replace(":", ""),
                      date=str(r.get("date")) if r.get("date") else None, source="manual")
                 for r in (games.get("pokemon") or [])]
    pk_res = results.get("pokemonsets")
    api_pk = pk_res["items"] if pk_res and pk_res["ok"] else [r for r in prev.get("tcg_releases", []) if r.get("source") != "manual"]
    # manual rows win by date match (same set), otherwise both are kept
    api_dates = {}
    for r in api_pk:
        api_dates.setdefault(r.get("date"), []).append(r)
    merged, used = [], set()
    for mrow in manual_pk:
        hit = next((h for h in api_dates.get(mrow.get("date")) or [] if h["id"] not in used), None)
        mrow.setdefault("category", "boosters")
        if hit:
            used.add(hit["id"])
            keep_id = mrow["id"]
            over = {k: v for k, v in mrow.items() if v not in (None, "")}
            mrow = dict(hit); mrow.update(over); mrow["id"] = keep_id; mrow["api_id"] = hit["id"]
        merged.append(mrow)
    new["tcg_releases"] = merged + [r for r in api_pk if r["id"] not in used]

    # Stock: replace rows from sources that ran; keep the rest from last time.
    if any(s in results for s in STOCK_SOURCES):
        ran = [s for s in STOCK_SOURCES if s in results]
        keep = [r for r in prev.get("stock", []) if r.get("src") not in ran and r.get("src")]
        fresh = []
        for s in ran:
            res = results[s]
            if res["ok"]:
                fresh += [dict(r, src=s, checked_at=now_iso()) for r in res["items"]]
            elif res["ok"] is False:  # keep last known rows for a failing retailer
                kept = [dict(r) for r in prev.get("stock", []) if r.get("src") == s
                        or (not r.get("src") and r.get("retailer") == RETAILER.get(s))]
                # a URL changed in the watchlist still reaches the kept row
                urls = [w.get("url") for w in cfg.get("stock", []) if w.get("retailer") == s and w.get("url")]
                if len(urls) == 1:
                    for r in kept:
                        if r.get("url") and r.get("url") != urls[0]:
                            r["url"] = urls[0]
                fresh += kept
        stock = keep + fresh + placeholders(cfg, results)
        # a live tracker row replaces the "not set up" row for the same retailer and item
        have = {(r.get("item"), (r.get("retailer") or "").split(" (")[0]) for r in stock if r.get("status") != "not_configured"}
        stock = [r for r in stock if r.get("status") != "not_configured"
                 or (r.get("item"), r.get("retailer")) not in have]
        # a first-hand check that worked this run beats the tracker's secondhand row
        firsthand = {(r.get("item"), r.get("retailer")) for r in stock
                     if r.get("src") in FIRST_HAND and (results.get(r.get("src")) or {}).get("ok")
                     and r.get("status") not in ("not_configured", "unknown") and not r.get("area")}
        new["stock"] = [r for r in stock if r.get("src") != "trackers"
                        or (r.get("item"), (r.get("retailer") or "").split(" (")[0]) not in firsthand]

    new.update(schema=2, generated_at=now_iso(), generated_by="poller",
               location=cfg.get("location") or new.get("location"), link_rules=LINK_RULES)
    gh = str(cfg.get("github_user") or "")
    new["calendar_url"] = (f"https://raw.githubusercontent.com/{gh}/trx/main/calendar.ics"
                           if gh and not gh.startswith("<") else None)
    # stock_log: every status flip, for the Log tab and restock predictions (last 500)
    before = {r.get("id"): r for r in prev.get("stock", [])}
    flips = []
    for r in new.get("stock", []):
        was = (before.get(r.get("id")) or {}).get("status")
        if (was is not None and was != r.get("status") and r.get("status") not in ("not_configured", "unknown")
                and was != "unknown"):
            flips.append({"at": now_iso(), "item": r.get("item"), "retailer": r.get("retailer"), "store": r.get("store"),
                          "area": r.get("area"), "from": was, "to": r.get("status"), "price": r.get("price"), "url": r.get("url")})
    # tracker history (TrackaLacker "Recent Changes") joins the log with its own timestamps
    tr = results.get("trackers")
    if tr and tr.get("ok") and tr.get("history"):
        known = {(f.get("at"), f.get("retailer"), f.get("to")) for f in (prev.get("stock_log") or []) + flips}
        for h in tr["history"]:
            key = (h["at"], h["retailer"], h["status"])
            if key not in known:
                known.add(key)
                flips.append({"at": h["at"], "item": h.get("item"), "retailer": h["retailer"], "store": None, "area": None,
                              "from": None, "to": h["status"], "price": h.get("price"), "url": h.get("url"),
                              "source": h.get("source"), "tracker_url": h.get("tracker_url")})
    # one-time cleanup: GameStop "in stock" readings on Sep 30, 2026 came from the page's placeholder
    # (read before its scripts loaded), so they were never real restocks
    old_log = [f for f in (prev.get("stock_log") or []) if not (f.get("retailer") == "GameStop" and not f.get("source")
               and str(f.get("at", "")).startswith("2026-09-30")
               and ({f.get("to"), f.get("from")} & {"in_stock", "preorder_live"}))]
    new["stock_log"] = sorted(flips + old_log, key=lambda f: f.get("at") or "", reverse=True)[:500]
    # Pokémon icons for raids, research, eggs and Rocket (cached run to run)
    try:
        new["icons_status"] = icons.build(new, prev, out)
    except Exception as e:
        new["icons"] = prev.get("icons") or {}
        out(f"icons: skipped ({type(e).__name__}: {str(e)[:80]})")
    # Pokémon Center queue reported live → one @here ping per 6 hours (secondhand: "verify")
    pq = new.get("pc_queue") or {}
    if pq.get("live"):
        last = pq.get("last_ping")
        fresh = not last or (now_utc() - dt.datetime.fromisoformat(last)).total_seconds() > 6 * 3600
        if fresh:
            srcs = sorted({p["source"] for p in pq.get("posts") or []})
            text = (f"{(cfg.get('notify') or {}).get('mention_on_first_hand', '')} 🟠 POKÉMON CENTER QUEUE reported live · "
                    f"{len(pq.get('posts') or [])} post(s) in the last 45 min ({', '.join(srcs)}) · verify\nhttps://www.pokemoncenter.com/").strip()
            if not dry and (cfg.get("notify") or {}).get("slack", True):
                try:
                    if alerts.slack(text):
                        pq["last_ping"] = now_iso()
                except Exception:
                    pass
            out("pcqueue: " + ("ping sent" if pq.get("last_ping") == now_iso() else ("would ping (dry run)" if dry else "ping not sent")))
    # Slack pings on stock changes (first-hand readings were double-checked by their sources)
    nt = notify.run(prev.get("stock", []), new.get("stock", []), cfg, dry=dry)
    out(f"notify: {nt['live']} in-stock change(s), {nt['gone']} back to sold out, slack {'sent' if nt['sent'] else 'not sent'}")
    lines, new["alerted"] = diff.compute(prev, new, cfg)
    if nt["text"]:
        lines = [ln for ln in lines if not ln.startswith("🟢")] + [nt["text"].replace("<!here>\n", "")]
    new["alerts_log"] = ([{"at": now_iso(), "line": ln} for ln in lines] + list(prev.get("alerts_log") or []))[:30]
    out(f"diff: {len(lines)} alert line(s)")
    for line in lines:
        out("  " + line)
    new["runs_24h"] = runs_24h(out) or prev.get("runs_24h")
    if dry:
        out("dry run: nothing written, nothing sent")
        return new, lines
    prev_gen = parse_iso(prev.get("generated_at"))
    stale = prev_gen is None or (now_utc() - prev_gen).total_seconds() >= HEARTBEAT_HOURS * 3600
    if signature(prev) == signature(new) and not stale and not lines:
        out("drive: unchanged, not written (heartbeat in "
            f"{HEARTBEAT_HOURS * 3600 - (now_utc() - prev_gen).total_seconds():.0f}s)")
        return new, lines
    size = drive.write_dashboard(new)
    out(f"drive: wrote {size / 1024:.1f} KB" + (" (heartbeat)" if stale and signature(prev) == signature(new) else ""))
    changed, n_ev = ics.write(new, cfg, ROOT)
    out(f"calendar: {n_ev} entries, {'updated' if changed else 'unchanged'}")
    sent = alerts.discord(lines)
    out(f"discord: {sent} message(s) sent")
    return new, lines


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args(argv)
    if a.test_alert:
        s = alerts.slack("✅ TRX test ping: stock alerts will post here.")
        print("slack: test ping sent" if s else "slack: SLACK_WEBHOOK not set")
        d = alerts.discord(["✅ TRX test alert: the Discord webhook works."])
        print(f"discord: {d} test message(s) sent" if d else "discord: DISCORD_WEBHOOK not set")
        return 0 if (s or d) else 1
    run(only=a.only, force=a.force or os.environ.get("FORCE") == "true", dry=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
