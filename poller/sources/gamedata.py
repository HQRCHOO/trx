"""Calculated counters, best attackers per type, and CP data from the game's own numbers.

- PokeMiners/game_masters (latest.json, ~19 MB): raid (PvE) move stats — power, duration, energy — and the
  CP multiplier table.
- pvpoke/pvpoke gamemaster.json: every Pokémon form with base stats, types, movesets, elite moves and a
  released flag (so unreleased Pokémon, Megas and Shadows are left out), plus move names.

Counters use a simplified estimate: damage per second over a fast/charged cycle at level 40 with perfect
IVs (Shadow ×1.2 attack, ×0.83 defense, STAB ×1.2, Pokémon GO type multipliers), weighted with bulk
(score = DPS³ × TDO, TDO ∝ DPS × HP × DEF). It ignores the boss's own moves, dodging, weather and friendship.
"""
import math
import re

from ..util import get, result

GM = "https://raw.githubusercontent.com/PokeMiners/game_masters/master/latest/latest.json"
PVP = "https://raw.githubusercontent.com/pvpoke/pvpoke/master/src/data/gamemaster.json"
RAIDS = "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/raids.json"
ROCKET = "https://raw.githubusercontent.com/bigfoott/ScrapedDuck/data/rocketLineups.json"

TCHART = {
    "normal": ([], ["rock", "steel"], ["ghost"]), "fire": (["grass", "ice", "bug", "steel"], ["fire", "water", "rock", "dragon"], []),
    "water": (["fire", "ground", "rock"], ["water", "grass", "dragon"], []), "electric": (["water", "flying"], ["electric", "grass", "dragon"], ["ground"]),
    "grass": (["water", "ground", "rock"], ["fire", "grass", "poison", "flying", "bug", "dragon", "steel"], []),
    "ice": (["grass", "ground", "flying", "dragon"], ["fire", "water", "ice", "steel"], []),
    "fighting": (["normal", "ice", "rock", "dark", "steel"], ["poison", "flying", "psychic", "bug", "fairy"], ["ghost"]),
    "poison": (["grass", "fairy"], ["poison", "ground", "rock", "ghost"], ["steel"]),
    "ground": (["fire", "electric", "poison", "rock", "steel"], ["grass", "bug"], ["flying"]),
    "flying": (["grass", "fighting", "bug"], ["electric", "rock", "steel"], []), "psychic": (["fighting", "poison"], ["psychic", "steel"], ["dark"]),
    "bug": (["grass", "psychic", "dark"], ["fire", "fighting", "poison", "flying", "ghost", "steel", "fairy"], []),
    "rock": (["fire", "ice", "flying", "bug"], ["fighting", "ground", "steel"], []), "ghost": (["psychic", "ghost"], ["dark"], ["normal"]),
    "dragon": (["dragon"], ["steel"], ["fairy"]), "dark": (["psychic", "ghost"], ["fighting", "dark", "fairy"], []),
    "steel": (["ice", "rock", "fairy"], ["fire", "water", "electric", "steel"], []), "fairy": (["fighting", "dragon", "dark"], ["fire", "poison", "steel"], []),
}
BOSS_DEF = 200.0   # generic raid-boss defense; a constant factor, so it doesn't change the order
TOP = 6


def eff(att, defs):
    m = 1.0
    for d in defs:
        se, nve, imm = TCHART.get(att, ([], [], []))
        m *= 1.6 if d in se else 0.625 if d in nve else 0.390625 if d in imm else 1.0
    return m


def display_name(species):
    n = species
    for tag, pre in (("Mega X", "Mega "), ("Mega Y", "Mega "), ("Mega", "Mega "), ("Primal", "Primal "), ("Shadow", "Shadow ")):
        m = re.match(r"^(.*) \(" + tag + r"\)$", n)
        if m:
            return pre + m.group(1) + (" " + tag.split()[-1] if tag.startswith("Mega ") else "")
    return n


def load():
    gm = get(GM, timeout=120).json()
    pvp = get(PVP, timeout=60).json()
    pve, cpm = {}, []
    for t in gm:
        tid = t.get("templateId", "")
        dat = t.get("data") or {}
        ms = dat.get("moveSettings")
        if ms and re.match(r"^V\d+_MOVE_", tid):
            mid = str(ms.get("movementId") or "").replace("_FAST", "")
            pve[mid] = {"type": str(ms.get("pokemonType", "")).replace("POKEMON_TYPE_", "").lower(),
                        "power": float(ms.get("power") or 0), "dur": float(ms.get("durationMs") or 1000) / 1000.0,
                        "energy": float(ms.get("energyDelta") or 0)}
        if tid == "PLAYER_LEVEL_SETTINGS":
            cpm = (dat.get("playerLevel") or {}).get("cpMultiplier") or []
    names = {m["moveId"]: m.get("name") or m["moveId"].title() for m in pvp.get("moves") or []}
    mons = []
    for p in pvp.get("pokemon") or []:
        if not p.get("released") or p.get("aliasId"):
            continue
        tags = p.get("tags") or []
        mons.append({"id": p["speciesId"], "name": display_name(p.get("speciesName") or p["speciesId"]),
                     "types": [t for t in p.get("types") or [] if t and t != "none"],
                     "atk": p["baseStats"]["atk"], "def": p["baseStats"]["def"], "hp": p["baseStats"]["hp"],
                     "fast": p.get("fastMoves") or [], "charged": p.get("chargedMoves") or [],
                     "elite": set((p.get("eliteMoves") or []) + (p.get("legacyMoves") or [])),
                     "shadow": "shadow" in tags, "mega": "mega" in tags,
                     "legend": bool({"legendary", "mythical", "ultrabeast"} & set(tags))})
    return pve, cpm, names, mons


def best_moveset(m, defs, pve, cpm40, only_type=None):
    a = (m["atk"] + 15) * cpm40 * (1.2 if m["shadow"] else 1.0)
    best = None
    for f in m["fast"]:
        fm = pve.get(f)
        if not fm or fm["energy"] <= 0 or (only_type and fm["type"] != only_type):
            continue
        fd = math.floor(0.5 * fm["power"] * a / BOSS_DEF * (1.2 if fm["type"] in m["types"] else 1) * eff(fm["type"], defs)) + 1
        for c in m["charged"]:
            cm = pve.get(c)
            if not cm or cm["energy"] >= 0 or (only_type and cm["type"] != only_type):
                continue
            cd = math.floor(0.5 * cm["power"] * a / BOSS_DEF * (1.2 if cm["type"] in m["types"] else 1) * eff(cm["type"], defs)) + 1
            n = math.ceil(-cm["energy"] / fm["energy"])
            dps = max((n * fd + cd) / (n * fm["dur"] + cm["dur"]), fd / fm["dur"])
            if not best or dps > best[0]:
                best = (dps, f, c)
    if not best:
        return None
    hp = math.floor((m["hp"] + 15) * cpm40)
    d = (m["def"] + 15) * cpm40 * (0.8333 if m["shadow"] else 1.0)
    return {"dps": best[0], "score": best[0] ** 4 * hp * d, "fast": best[1], "charged": best[2]}


def rank(defs, mons, pve, cpm40, names, top=TOP, only_type=None):
    rows = []
    for m in mons:
        b = best_moveset(m, defs, pve, cpm40, only_type)
        if b:
            rows.append((b["score"], m, b))
    rows.sort(key=lambda r: -r[0])
    out, seen = [], set()
    for score, m, b in rows:
        if m["name"] in seen:
            continue
        seen.add(m["name"])
        out.append({"name": m["name"], "fast": names.get(b["fast"], b["fast"]), "charged": names.get(b["charged"], b["charged"]),
                    "elite": [x for x in (names.get(b["fast"], b["fast"]) if b["fast"] in m["elite"] else None,
                                          names.get(b["charged"], b["charged"]) if b["charged"] in m["elite"] else None) if x]})
        if len(out) >= top:
            break
    return out


# Tiers by how reachable a Pokémon is (v3.43). Each tier skips Pokémon already shown in a higher tier.
TIERS = (
    ("S", 2, lambda m: True, False),                                                     # best possible
    ("A", 3, lambda m: m["legend"] and not m["mega"] and not m["shadow"], False),        # legendaries from raids
    ("B", 3, lambda m: not m["legend"] and not m["mega"], False),                        # non-legendary, Shadows OK
    ("C", 3, lambda m: not m["legend"] and not m["mega"] and not m["shadow"], True),     # budget, no Elite TM moves
)


RANK_N = 40


def tiers(defs, mons, pve, cpm40, names):
    def ranked(pred, no_elite):
        rows = []
        for m in mons:
            if not pred(m):
                continue
            mm = dict(m, fast=[f for f in m["fast"] if f not in m["elite"]],
                      charged=[c for c in m["charged"] if c not in m["elite"]]) if no_elite else m
            b = best_moveset(mm, defs, pve, cpm40)
            if b:
                rows.append((b["score"], m, b))
        return sorted(rows, key=lambda r: -r[0])
    best = ranked(lambda m: True, False)
    if not best:
        return {}
    top, used, out = best[0][0], set(), {}
    for tier, n, pred, no_elite in TIERS:
        picks = []
        for score, m, b in (best if tier == "S" else ranked(pred, no_elite)):
            if m["name"] in used:
                continue
            used.add(m["name"])
            fast, charged = names.get(b["fast"], b["fast"]), names.get(b["charged"], b["charged"])
            picks.append({"name": m["name"], "fast": fast, "charged": charged,
                          "ft": (pve.get(b["fast"]) or {}).get("type"), "ct": (pve.get(b["charged"]) or {}).get("type"),
                          "elite": [x for x, mid in ((fast, b["fast"]), (charged, b["charged"])) if mid in m["elite"]],
                          "pct": round((score / top) ** 0.25 * 100)})
            if len(picks) >= n:
                break
        out[tier] = picks
    # v3.65: a longer ranked list of regular attackers (no Megas, no Shadows) so the page can find the
    # rider's own best Pokémon for this boss. Compact keys: n name, f/c moves, ft/ct move types, p % of top, e elite.
    rank, seen = [], set()
    for score, m, b in ranked(lambda m: not m["mega"] and not m["shadow"], False):
        if m["name"] in seen:
            continue
        seen.add(m["name"])
        rank.append({"n": m["name"], "f": names.get(b["fast"], b["fast"]), "c": names.get(b["charged"], b["charged"]),
                     "ft": (pve.get(b["fast"]) or {}).get("type"), "ct": (pve.get(b["charged"]) or {}).get("type"),
                     "p": round((score / top) ** 0.25 * 100), "e": 1 if (b["fast"] in m["elite"] or b["charged"] in m["elite"]) else 0})
        if len(rank) >= RANK_N:
            break
    out["rank"] = rank
    return out


def _pick(m, b, names):
    fast, charged = names.get(b["fast"], b["fast"]), names.get(b["charged"], b["charged"])
    return {"name": m["name"], "fast": fast, "charged": charged, "ft": None, "ct": None,
            "elite": [x for x, mid in ((fast, b["fast"]), (charged, b["charged"])) if mid in m["elite"]]}


def rocket_teams(mons, pve, cpm40, names):
    """For every Rocket lineup: an easy team (regular non-legendary Pokémon, no Shadows, no Elite TM moves)
    and a power team (legendaries and Shadows OK),
    one attacker per slot. Each slot's pick is the attacker with the best average showing against every
    Pokémon that can appear in that slot (score relative to the best possible answer for each). Megas are
    left out because they can't be used in Team GO Rocket battles."""
    lineups = get(ROCKET).json()
    pool = [m for m in mons if not m["mega"]]
    combos = {}
    for g in lineups:
        for key in ("firstPokemon", "secondPokemon", "thirdPokemon"):
            for p in g.get(key) or []:
                c = tuple(sorted(t.lower() for t in (p.get("types") or []) if t))
                if c:
                    combos.setdefault(c, None)
    easy_idx = [i for i, m in enumerate(pool) if not m["legend"] and not m["shadow"]]
    all_idx = list(range(len(pool)))
    plain = {i: dict(pool[i], fast=[f for f in pool[i]["fast"] if f not in pool[i]["elite"]],
                     charged=[c for c in pool[i]["charged"] if c not in pool[i]["elite"]]) for i in easy_idx}
    # score every attacker once per type combination (easy picks use only their regular moves)
    table, etable = {}, {}
    for c in combos:
        table[c] = [best_moveset(m, list(c), pve, cpm40) for m in pool]
        etable[c] = {i: best_moveset(plain[i], list(c), pve, cpm40) for i in easy_idx}
    best = {c: {"easy": max((b["score"] for b in etable[c].values() if b), default=1),
                "power": max((b["score"] for b in table[c] if b), default=1)} for c in table}

    def ranked(cands, which, idxs):
        rows = []
        for i in idxs:
            rel, bs = 0.0, None
            for c in cands:
                b = etable[c].get(i) if which == "easy" else table[c][i]
                if not b:
                    rel = -1
                    break
                rel += (b["score"] / best[c][which]) ** 0.25
                if bs is None or b["score"] > bs["score"]:
                    bs = b
            if rel > 0:
                rows.append((rel / len(cands), i, bs))
        rows.sort(key=lambda r: -r[0])
        return rows

    out = {}
    for g in lineups:
        slots = []
        for key in ("firstPokemon", "secondPokemon", "thirdPokemon"):
            cands = sorted({tuple(sorted(t.lower() for t in (p.get("types") or []) if t)) for p in g.get(key) or []} - {()})
            slots.append(cands)
        res = {}
        for which, idxs in (("easy", easy_idx), ("power", all_idx)):
            def mk(m, b, rel):
                return dict(_pick(m, b, names), ft=pve.get(b["fast"], {}).get("type"), ct=pve.get(b["charged"], {}).get("type"), pct=round(rel * 100))
            rankings = [ranked(c, which, idxs) if c else [] for c in slots]
            team, used = [], set()
            for r in rankings:                      # 1) the team: best available per slot, all different
                pick = next(((rel, i, b) for rel, i, b in r if pool[i]["name"] not in used), None)
                if pick:
                    used.add(pool[pick[1]]["name"]); team.append(mk(pool[pick[1]], pick[2], pick[0]))
                else:
                    team.append(None)
            backups, shown = [], set(used)
            for r in rankings:                      # 2) backups: never a team member, never repeated
                alts = []
                for rel, i, b in r:
                    if pool[i]["name"] in shown:
                        continue
                    shown.add(pool[i]["name"]); alts.append(mk(pool[i], b, rel))
                    if len(alts) >= 2:
                        break
                backups.append(alts)
            res[which] = {"team": team, "backups": backups}
        out[g.get("name")] = res
    return out


def fetch(cfg, prev):
    pve, cpm, names, mons = load()
    cpm40 = cpm[39] if len(cpm) > 39 else 0.7903
    counters = {}
    for r in get(RAIDS).json():
        defs = [t.get("name") for t in r.get("types") or [] if isinstance(t, dict)]
        img = str(r.get("image") or "").rsplit("/", 1)[-1] or r.get("name")
        key = ("shadow:" if str(r.get("name", "")).lower().startswith("shadow ") else "") + img
        if defs:
            counters[key] = tiers(defs, mons, pve, cpm40, names)
    type_top = {}
    for t in TCHART:
        neutral = next(d for d in TCHART if eff(t, [d]) == 1.0)
        type_top[t] = [x["name"] for x in rank([neutral], mons, pve, cpm40, names, top=5, only_type=t)]
    dex = sorted({(m["name"], m["atk"], m["def"], m["hp"], "/".join(m["types"])) for m in mons if not m["mega"] and not m["shadow"]})
    try:
        rteams = rocket_teams(mons, pve, cpm40, names)
    except Exception:
        rteams = (prev.get("gamedata") or {}).get("rocket_teams") or {}
    return result([], counters=counters, type_top=type_top, dex=[list(x) for x in dex], cpm=[round(x, 7) for x in cpm[:51]], rocket_teams=rteams,
                  note=f"{len(mons)} released forms · {len(pve)} raid moves · counters for {len(counters)} bosses")
