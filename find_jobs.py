#!/usr/bin/env python3
"""Pull mechanical/aerospace jobs from the public jobs db and write a ranked CSV.

Source: DevelopIQ-ai/jobs_db -> Supabase table `ds_hiring_cafe` (public, read-only).

    python3 find_jobs.py                  # writes jobs_ranked.csv
    python3 find_jobs.py --min-score 4    # stricter
    python3 find_jobs.py --selftest       # offline check of the filter logic

Edit the CONFIG block to tune. Stdlib only.
"""
import argparse
import csv
import json
import re
import sys
import urllib.parse
import urllib.request

SUPABASE_URL = "https://snimonofzyrbsqovpnsq.supabase.co/rest/v1/ds_hiring_cafe"
ANON_KEY = "sb_publishable_TPu8VN6vPj2yKYAG-Z-_fg_LubsqKkn"  # public read-only key from jobs_db README

# ----------------------------- CONFIG ---------------------------------------

# Server-side prefilter: a job is fetched if its TITLE contains any of these.
TITLE_QUERY = [
    "mechanical", "aerospace", "propulsion", "turbine", "turbomachinery",
    "jet engine", "gas turbine", "structures", "stress", "thermal",
    "fluids", "cfd", "fea", "gd&t", "cad", "design engineer", "systems engineer",
    "flight", "airframe", "rocket", "launch", "spacecraft", "combustion",
    "aeromechanical", "aerothermal", "rotating equipment", "test engineer",
    "compressor", "turbocharger", "performance engineer", "hypersonic", "simulation engineer",
    "cycle", "validation engineer", "development engineer",
]

# Title keywords -> score. First tier = core fit.
TITLE_SCORES = {
    r"\b(mechanical|aerospace|aeronautical|propulsion|turbomachinery|aeromechanical|aerothermal)\b": 4,
    r"\b(turbine|jet engine|gas turbine|combustion|rotating equipment)\b": 4,
    r"\b(structures?|stress|airframe|thermal|fluids?|cfd|fea)\b": 3,
    r"\b(design engineer|cad|gd&t|test engineer|systems engineer|flight|rocket|launch|spacecraft)\b": 2,
    r"\b(compressor|turbocharger|turbo|hot gas|turbine test|engine test|performance engineer|cycle)\b": 3,
    r"\b(test|validation|development|simulation)\b.*\bengineer|\bengineer.*\b(test|validation|simulation)\b": 1,
    r"\bengineer\b": 1,
}

# Description keywords -> score (counted once each).
DESC_SCORES = {
    r"turbomachinery": 3, r"jet engine|gas turbine|turbofan|turbojet": 3,
    r"propulsion": 2, r"aerospace|aircraft|spacecraft|satellite": 2,
    r"solidworks|catia|nx\b|creo|siemens nx": 1, r"gd&t|gdt": 1,
    r"ansys|abaqus|nastran|fea\b|cfd\b": 1,
    r"compressor map|turbine map|hot gas|test stand|test cell|surge|performance map": 3,
    r"matlab|python": 1, r"amesim|gt-?suite|simulink|1d (?:system )?model": 1,
    r"additive manufacturing|slm|dmls|3d print": 1,
    r"hypersonic|rocket|liquid engine|turbopump|combustor|afterburner|power generation": 2, r"stress analysis|structural analysis": 2,
}

# Known aerospace / defense / engine employers: small bonus (substring match).
COMPANY_BONUS = [
    "boeing", "lockheed", "northrop", "raytheon", "rtx", "pratt", "ge aerospace",
    "general electric", "honeywell", "spacex", "blue origin", "rocket lab",
    "relativity", "sierra space", "anduril", "joby", "archer", "beta technologies",
    "l3harris", "general dynamics", "bae systems", "textron", "gulfstream",
    "collins", "safran", "rolls-royce", "siemens energy", "solar turbines",
    "mitsubishi power", "baker hughes", "firefly", "united launch alliance", "stoke", "ursa major", "hermeus", "boom supersonic", "zero avia",
    "nasa", "jacobs", "leidos", "sierra nevada", "aerojet", "williams international", "howmet", "woodward", "elliott group",
    "capstone", "hamilton sundstrand", "mtu aero", "kratos", "vast", "varda", "impulse space",
    "true anomaly", "castelion", "radiant", "x-energy", "helion", "commonwealth fusion",
    "venus aerospace", "saronic", "divergent", "ball aerospace", "bwxt", "ge vernova",
    "ge aviation", "pratt & whitney", "abl space", "terran orbital", "intuitive machines",
    "ast spacemobile", "skydweller", "wisk", "overair",
]

# Titles to drop: too senior, or not a job-search target.
TITLE_EXCLUDE = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|vp|chief|"
    r"intern|internship|co-op|coop|apprentice|technician|mechanic|sales|"
    r"field service)\b", re.I)
SENIORITY_EXCLUDE = re.compile(r"senior|executive|director|lead|manager", re.I)

# Experience asks above your range, e.g. "8+ years".
YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?(?:years|yrs)", re.I)
MAX_YEARS_REQUIRED = 5

# Active clearance required at start (you're a US citizen without one).
# "Ability to obtain" / "eligible for" is fine and NOT excluded.
CLEARANCE_BLOCK = re.compile(
    r"(active|current|existing|must (?:have|hold|possess)|requires?|required)"
    r"[^.\n]{0,40}(secret|top secret|ts/sci|ts\s*/\s*sci|clearance|dod)", re.I)
CLEARANCE_OK = re.compile(
    r"(ability to obtain|able to obtain|eligib\w+ (?:for|to obtain)|can obtain|"
    r"willing(?:ness)? to obtain|obtain and maintain)", re.I)

# You are leaving automotive: hard-exclude these employers and any automotive industry tag.
AUTOMOTIVE_COMPANIES = [
    "general motors", "gm ", "ford motor", "stellantis", "chrysler", "tesla", "rivian",
    "lucid", "toyota", "honda", "nissan", "hyundai", "kia", "bmw", "mercedes", "volkswagen",
    "audi", "volvo", "gm cruise", "cummins", "paccar", "navistar", "daimler truck",
    "borgwarner", "garrett motion", "aptiv", "lear corp", "magna", "dana inc", "tenneco",
    "bosch", "continental", "denso", "zf ", "allison transmission", "harley", "polaris",
    "fisker", "polestar", "nikola", "lordstown", "harman",
]
AUTOMOTIVE_INDUSTRY = re.compile(r"automotive|motor vehicle|auto parts", re.I)
AUTO_DESC = re.compile(r"\b(automotive|passenger vehicle|oem vehicle|light[- ]duty|powertrain)\b", re.I)

# State preference (bonus, not a filter). Colorado + neighbours, FL, NC, SC, TX.
PREFERRED_STATES = {
    "colorado": 3, "texas": 3, "florida": 3, "north carolina": 3, "south carolina": 3,
    "wyoming": 2, "nebraska": 2, "kansas": 2, "oklahoma": 2, "new mexico": 2,
    "utah": 2, "arizona": 1,
}

MIN_SALARY = 120_000   # soft floor: unlisted pay is kept; listed max below this is dropped
EXCLUDE_STATES = ("california",)          # lowercase full names
EXCLUDE_STATE_ABBR = ("CA",)

# ----------------------------------------------------------------------------


def fetch(page_size=1000, max_rows=20000):
    ors = ",".join(f"title.ilike.*{urllib.parse.quote(t)}*" for t in TITLE_QUERY)
    base = f"{SUPABASE_URL}?select=*&or=({ors})&order=scraped_at.desc"
    rows, start = [], 0
    while start < max_rows:
        req = urllib.request.Request(base, headers={
            "apikey": ANON_KEY, "Authorization": f"Bearer {ANON_KEY}",
            "Range-Unit": "items", "Range": f"{start}-{start + page_size - 1}",
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            batch = json.load(r)
        rows += batch
        print(f"fetched {len(rows)} rows", file=sys.stderr)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


def loc_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [s for s in re.split(r"[;|]", str(v)) if s.strip()]


def is_california(loc):
    low = loc.lower()
    return any(s in low for s in EXCLUDE_STATES) or any(
        re.search(rf",\s*{a}\b", loc) for a in EXCLUDE_STATE_ABBR)


def evaluate(row):
    """Return (keep, score, notes)."""
    title = row.get("title") or ""
    desc = (row.get("description_md") or "")
    company = (row.get("company_name") or "").lower()
    notes = []

    if TITLE_EXCLUDE.search(title):
        return False, 0, ["title excluded"]
    if SENIORITY_EXCLUDE.search(str(row.get("seniority_level") or "")):
        return False, 0, ["seniority too high"]

    # Automotive: hard exclude by employer / industry; soften on heavy description use.
    ind = row.get("company_industry")
    ind = " ".join(map(str, ind)) if isinstance(ind, list) else str(ind or "")
    if any(c in company + " " for c in AUTOMOTIVE_COMPANIES) or AUTOMOTIVE_INDUSTRY.search(ind):
        return False, 0, ["automotive"]
    auto_hits = len(AUTO_DESC.findall(desc))

    # Location: drop only if every listed location is California.
    locs = loc_list(row.get("locations"))
    wt = str(row.get("workplace_type") or "")
    if locs and all(is_california(l) for l in locs):
        return False, 0, ["california only"]
    if any(is_california(l) for l in locs):
        notes.append("also lists CA")

    # Salary: soft floor.
    smax, smin = row.get("salary_max_yearly"), row.get("salary_min_yearly")
    top = smax or smin
    if top is not None and top < MIN_SALARY:
        return False, 0, ["salary below floor"]

    # Clearance: block only when an active clearance is demanded up front.
    m = CLEARANCE_BLOCK.search(desc)
    if m and not CLEARANCE_OK.search(desc[max(0, m.start() - 80): m.end() + 80]):
        return False, 0, ["active clearance required"]
    if re.search(r"clearance|u\.?s\.? citizen|itar", desc, re.I):
        notes.append("citizenship/clearance mentioned")

    # Experience requirement above range.
    yrs = [int(y) for y in YEARS_RE.findall(desc)]
    if yrs and min(yrs) > MAX_YEARS_REQUIRED:
        return False, 0, [f"asks {min(yrs)}+ yrs"]

    # Scoring.
    score = 0
    for pat, pts in TITLE_SCORES.items():
        if re.search(pat, title, re.I):
            score += pts
    for pat, pts in DESC_SCORES.items():
        if re.search(pat, desc, re.I):
            score += pts
    if any(c in company for c in COMPANY_BONUS):
        score += 2
        notes.append("target employer")
    if top is not None:
        score += 2
        notes.append("salary listed")
    else:
        notes.append("no salary listed")
    if "remote" in wt.lower():
        score += 1
    locblob = " ".join(locs).lower()
    best = max([pts for st, pts in PREFERRED_STATES.items() if st in locblob], default=0)
    if best:
        score += best
        notes.append("preferred state")
    if auto_hits >= 3:
        score -= 3
        notes.append("automotive-heavy description")
    return True, score, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="jobs_ranked.csv")
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--max-rows", type=int, default=20000)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    kept, dropped = [], {}
    for row in fetch(max_rows=a.max_rows):
        ok, score, notes = evaluate(row)
        if not ok:
            dropped[notes[0]] = dropped.get(notes[0], 0) + 1
        elif score >= a.min_score:
            kept.append((score, notes, row))
        else:
            dropped["low score"] = dropped.get("low score", 0) + 1

    kept.sort(key=lambda t: (-t[0], -(t[2].get("salary_max_yearly") or 0)))
    cols = ["score", "title", "company_name", "locations", "workplace_type",
            "seniority_level", "commitment", "salary_min_yearly",
            "salary_max_yearly", "notes", "apply_url"]
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for score, notes, r in kept:
            w.writerow([score, r.get("title"), r.get("company_name"),
                        "; ".join(loc_list(r.get("locations"))), r.get("workplace_type"),
                        r.get("seniority_level"), r.get("commitment"),
                        r.get("salary_min_yearly"), r.get("salary_max_yearly"),
                        ", ".join(notes), r.get("apply_url")])
    print(f"\nkept {len(kept)} -> {a.out}\ndropped: {dropped}", file=sys.stderr)


def selftest():
    base = dict(workplace_type="Onsite", seniority_level="Entry Level",
                description_md="Design turbine blades. SolidWorks, GD&T.",
                company_name="Acme", salary_max_yearly=None, salary_min_yearly=None)
    cases = [
        ("keeps entry turbomachinery role", dict(title="Turbomachinery Engineer", locations=["Cincinnati, Ohio, US"]), True),
        ("drops senior title", dict(title="Senior Mechanical Engineer", locations=["Ohio"]), False),
        ("drops CA-only", dict(title="Mechanical Engineer", locations=["Hawthorne, California, US"]), False),
        ("keeps CA + other", dict(title="Mechanical Engineer", locations=["Hawthorne, California, US", "Austin, Texas, US"]), True),
        ("drops low listed salary", dict(title="Mechanical Engineer", locations=["Texas"], salary_max_yearly=90000), False),
        ("keeps unlisted salary", dict(title="Mechanical Engineer", locations=["Texas"]), True),
        ("drops active clearance", dict(title="Aerospace Engineer", locations=["Texas"], description_md="Active Secret clearance required."), False),
        ("keeps ability to obtain", dict(title="Aerospace Engineer", locations=["Texas"], description_md="Must be able to obtain a Secret clearance; active clearance not required... ability to obtain a clearance."), True),
        ("keeps US-citizen-only", dict(title="Aerospace Engineer", locations=["Texas"], description_md="Must be a U.S. citizen (ITAR)."), True),
        ("drops 8+ years", dict(title="Mechanical Engineer", locations=["Texas"], description_md="8+ years of experience required."), False),
        ("drops GM", dict(title="Turbocharger Engineer", company_name="General Motors", locations=["Michigan"]), False),
        ("drops auto industry", dict(title="Mechanical Engineer", company_industry=["Automotive"], locations=["Ohio"]), False),
        ("drops intern", dict(title="Mechanical Engineering Intern", locations=["Texas"]), False),
    ]
    bad = 0
    for name, over, want in cases:
        ok, score, notes = evaluate({**base, **over})
        flag = "ok " if ok == want else "FAIL"
        bad += ok != want
        print(f"{flag} {name}: keep={ok} score={score} {notes}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
