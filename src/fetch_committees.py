from __future__ import annotations

"""
Fetch current committee assignments from the unitedstates/congress-legislators
project and link them to the members in our ledger.

  legislators-current.json        bioguide + name + current term (chamber/state/district)
  committees-current.json         committee id -> name, type, subcommittees, jurisdiction
  committee-membership-current.json  committee id -> [members with bioguide]

Match each ledger member -> bioguide:
  House  on (state, district)   — effectively a unique key
  Senate on last name (+ first) — ~100 people, mostly unique last names

Output data/committees.json: per member their committees + the industry groups
those committees have jurisdiction over (via taxonomy.committee_to_industries).

Usage:
  python src/fetch_committees.py
"""

import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from taxonomy import committee_to_industries
from utils import CACHE_DIR, DATA_DIR, http_session, load_json, save_json, setup_logging

log = setup_logging("fetch_committees")

BASE = "https://unitedstates.github.io/congress-legislators"
FILES = {
    "legislators": "legislators-current.json",
    "committees": "committees-current.json",
    "membership": "committee-membership-current.json",
}
COMMITTEE_CACHE = CACHE_DIR / "committees"
LEDGER_PATH = DATA_DIR / "transactions.json"
OUT_PATH = DATA_DIR / "committees.json"
ALIASES_PATH = DATA_DIR / "member_aliases.json"
CACHE_TTL_DAYS = 7

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _norm(s: str) -> str:
    s = (s or "").lower().replace(".", " ")
    s = re.sub(r"[^a-z\s]", " ", s)
    toks = [t for t in s.split() if t and t not in _SUFFIXES]
    return " ".join(toks)


def _last_name_candidates(full_name: str) -> list[str]:
    """Best-effort last-name guesses from a 'First [Middle] Last' string."""
    toks = _norm(full_name).split()
    if not toks:
        return []
    cands = [toks[-1]]
    if len(toks) >= 2:
        cands.append(" ".join(toks[-2:]))  # e.g. 'van hollen', 'cortez masto'
    return cands


def _download(session, kind: str):
    cache = COMMITTEE_CACHE / FILES[kind]
    if cache.exists():
        age = (datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime)).days
        if age < CACHE_TTL_DAYS:
            return load_json(cache)
    log.info("Downloading %s", FILES[kind])
    data = session.get(f"{BASE}/{FILES[kind]}", timeout=60).json()
    save_json(cache, data)
    return data


def build_indexes(legislators: list):
    house = {}        # (state, district:int) -> bioguide
    sen_by_last = {}  # last_norm -> [bioguide]
    meta = {}         # bioguide -> {name, first, state, chamber}
    for leg in legislators:
        bio = leg["id"]["bioguide"]
        term = leg["terms"][-1]
        nm = leg["name"]
        meta[bio] = {"name": nm.get("official_full") or f"{nm.get('first','')} {nm.get('last','')}".strip(),
                     "first": _norm(nm.get("nickname") or nm.get("first", "")),
                     "last": _norm(nm.get("last", "")), "state": term.get("state"),
                     "chamber": "house" if term.get("type") == "rep" else "senate"}
        if term.get("type") == "rep":
            d = term.get("district")
            house[(term.get("state"), int(d) if d is not None else 0)] = bio
        elif term.get("type") == "sen":
            sen_by_last.setdefault(_norm(nm.get("last", "")), []).append(bio)
    return house, sen_by_last, meta


def build_committee_ref(committees: list) -> dict:
    ref = {}
    for c in committees:
        cid = c.get("thomas_id")
        if not cid:
            continue
        ref[cid] = {"id": cid, "name": c.get("name", cid), "type": c.get("type"),
                    "is_sub": False, "parent": None}
        for sub in c.get("subcommittees") or []:
            sid = cid + sub.get("thomas_id", "")
            ref[sid] = {"id": sid, "name": f"{c.get('name','')}: {sub.get('name','')}",
                        "sub_name": sub.get("name", ""), "type": c.get("type"),
                        "is_sub": True, "parent": cid}
    return ref


def invert_membership(membership: dict) -> dict:
    bio_to_committees = {}
    for cid, members in membership.items():
        for m in members:
            bio = m.get("bioguide")
            if bio:
                bio_to_committees.setdefault(bio, []).append({"id": cid, "title": m.get("title")})
    return bio_to_committees


def match_member(row: dict, house, sen_by_last, meta) -> tuple[str | None, str]:
    chamber = row["chamber"]
    if chamber == "house":
        state, dist = row.get("state"), row.get("district")
        try:
            key = (state, int(dist) if dist not in (None, "") else 0)
        except ValueError:
            key = None
        if key and key in house:
            return house[key], "state+district"
    # Senate (or House fallthrough) — match by last name.
    for cand in _last_name_candidates(row["member"]):
        bios = sen_by_last.get(cand)
        if not bios:
            continue
        if len(bios) == 1:
            return bios[0], "last_name"
        first = _norm(row["member"]).split()[0] if _norm(row["member"]) else ""
        for b in bios:
            if first and meta[b]["first"] and first[0] == meta[b]["first"][0]:
                return b, "last+first"
        return bios[0], "last_name_ambiguous"
    return None, "unmatched"


def run() -> None:
    if not LEDGER_PATH.exists():
        log.error("No ledger; run fetchers first")
        return
    session = http_session()
    legislators = _download(session, "legislators")
    committees = _download(session, "committees")
    membership = _download(session, "membership")

    house, sen_by_last, meta = build_indexes(legislators)
    committee_ref = build_committee_ref(committees)
    bio_to_committees = invert_membership(membership)

    # Load the ledger (dict) so we can both index members and rewrite merges.
    ledger = load_json(LEDGER_PATH)
    rows = list(ledger.values())
    by_member: dict[str, dict] = {}
    for r in rows:
        by_member.setdefault(r["member_id"], r)
    row_counts = Counter(r["member_id"] for r in rows)

    # Match every member_id to a bioguide.
    bio_of, method_of = {}, {}
    for member_id, row in by_member.items():
        bio, method = match_member(row, house, sen_by_last, meta)
        bio_of[member_id], method_of[member_id] = bio, method

    # Merge member_ids that resolve to the same person (bioguide) — e.g. a name
    # recorded two slightly different ways. Canonical = the variant with the most
    # rows; alias the others to it and rewrite the ledger.
    bio_groups = defaultdict(list)
    for mid, bio in bio_of.items():
        if bio:
            bio_groups[bio].append(mid)
    alias: dict[str, str] = {}
    for bio, mids in bio_groups.items():
        if len(mids) < 2:
            continue
        canonical = max(mids, key=lambda m: row_counts[m])
        for m in mids:
            if m != canonical:
                alias[m] = canonical
    if alias:
        canon_name = {by_member[v]["member_id"]: by_member[v]["member"] for v in set(alias.values())}
        n_rows = 0
        for tx in ledger.values():
            c = alias.get(tx["member_id"])
            if c:
                tx["member_id"] = c
                tx["member"] = canon_name.get(c, tx["member"])
                n_rows += 1
        save_json(LEDGER_PATH, ledger)
        log.info("Merged %d duplicate member name-variant(s) into canonical members (%d rows rewritten)",
                 len(alias), n_rows)

    # Persist the accumulated variant→canonical map so old report links can redirect
    # (the variant id vanishes from the ledger after the rewrite, so we must remember it).
    acc = load_json(ALIASES_PATH) if ALIASES_PATH.exists() else {}
    acc.update(alias)
    for k in list(acc):                         # collapse any chains to the final canonical
        seen = set()
        while acc.get(k) in acc and acc[k] not in seen and acc[acc[k]] != acc[k]:
            seen.add(acc[k]); acc[k] = acc[acc[k]]
    if acc:
        save_json(ALIASES_PATH, acc)

    # Build committee entries keyed by canonical member_id (variants collapsed).
    out_members = {}
    matched = 0
    for member_id in by_member:
        cid = alias.get(member_id, member_id)
        if cid in out_members:
            continue
        bio = bio_of.get(cid)
        entry = {"bioguide": bio, "match_method": method_of.get(cid, "unmatched"),
                 "matched_name": None, "committees": [], "industries": []}
        if bio:
            matched += 1
            entry["matched_name"] = meta.get(bio, {}).get("name")
            inds: set[str] = set()
            for cm in bio_to_committees.get(bio, []):
                ref = committee_ref.get(cm["id"])
                if not ref:
                    continue
                entry["committees"].append({"id": ref["id"], "name": ref["name"],
                                            "type": ref["type"], "is_sub": ref["is_sub"],
                                            "title": cm.get("title")})
                inds |= committee_to_industries(ref["id"], ref["name"])
            entry["industries"] = sorted(inds)
        out_members[cid] = entry

    total = len(out_members)
    save_json(OUT_PATH, {
        "generated": datetime.utcnow().date().isoformat(),
        "n_total": total, "n_matched": matched,
        "match_rate": round(100 * matched / total, 1) if total else 0,
        "members": out_members,
        "committee_ref": committee_ref,
    })
    log.info("Committees: matched %d/%d members (%.1f%%); %d committees in reference",
             matched, total, 100 * matched / total if total else 0, len(committee_ref))


if __name__ == "__main__":
    run()
