#!/usr/bin/env python3
"""Find hiring signals on public job boards. Standard library only.

Boards: Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Breezy HR and Personio.

Usage:
    python3 hiring.py <company or domain> [more ...] [--keywords "sales,business development"]
                      [--days 180] [--json] [--signal-id hiring_bd]

For each company it tries likely board names on each board (for "Acme Labs": acmelabs, acme-labs,
acme; for "acme.io": acme) and lists open roles, newest first, with the posting date and link.
--keywords keeps roles whose title contains one of the words. --days keeps roles posted in the
last N days. --json prints one object per role, including the board name it matched, so you can
check it is the right company. --signal-id also prints each role as a leads.json signal:
    {"id": "hiring_bd", "date": "YYYY-MM-DD", "detail": "Hiring: <title> (<location>)", "source": URL}

A company with no board on these systems returns nothing: that is "unknown", not "not hiring".
Exit code 3 means no job board could be reached at all (network blocked): treat hiring as unknown.
"""
from __future__ import annotations

import argparse
import datetime as dt
import http.client
import json
import re
import sys
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

UA = "lead-finder/1.0 (+https://machgtm.com)"
TIMEOUT = 12
NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException)

Fetch = Callable[[str], "object | None"]


def http_json(url: str):
    """GET a URL. Returns parsed JSON, or the raw text when it is not JSON (Personio serves XML),
    or None for 404 and similar 'no such board' answers. Other failures raise."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, application/xml"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 404, 410):
            return None
        raise
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


SUFFIXES = ("inc", "llc", "ltd", "limited", "gmbh", "labs", "technologies", "technology", "tech",
            "software", "solutions", "systems", "hq", "io", "ai", "co", "pvt")
DOMAIN = re.compile(r"^(https?://)?(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/.*)?$")


def slugs(name: str) -> list[str]:
    """Likely board names for a company name or domain. Never just the first word of a name."""
    text = unicodedata.normalize("NFKD", name.strip().lower()).encode("ascii", "ignore").decode()
    if " " not in text and DOMAIN.match(text):
        host = re.sub(r"^https?://", "", text).removeprefix("www.").split("/")[0]
        return [host.split(".")[0]]
    words = re.findall(r"[a-z0-9]+", text.replace("'", ""))
    if not words:
        return []
    out = ["".join(words), "-".join(words)]
    core = list(words)
    while len(core) > 1 and core[-1] in SUFFIXES:
        core.pop()
        out += ["".join(core), "-".join(core)]
    seen, uniq = set(), []
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def to_date(value) -> str | None:
    """A valid YYYY-MM-DD posting date, or None."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):  # Lever: epoch milliseconds
            return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).date().isoformat()
        try:
            return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", str(value))
            return dt.date.fromisoformat(m.group(1)).isoformat() if m else None
    except (ValueError, OverflowError, OSError):
        return None


def greenhouse(slug: str, fetch: Fetch) -> list[dict] | None:
    data = fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return [{
        "board": "greenhouse",
        "title": j.get("title", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "date": to_date(j.get("first_published")),   # posting date only; updated_at moves on any edit
        "source": j.get("absolute_url", ""),
    } for j in data["jobs"]]


def lever(slug: str, fetch: Fetch) -> list[dict] | None:
    data = fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        return None
    return [{
        "board": "lever",
        "title": j.get("text", ""),
        "location": (j.get("categories") or {}).get("location", ""),
        "date": to_date(j.get("createdAt")),
        "source": j.get("hostedUrl", ""),
    } for j in data]


def ashby(slug: str, fetch: Fetch) -> list[dict] | None:
    data = fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    return [{
        "board": "ashby",
        "title": j.get("title", ""),
        "location": j.get("location", ""),
        "date": to_date(j.get("publishedAt") or j.get("publishedDate")),
        "source": j.get("jobUrl", ""),
    } for j in data["jobs"] if j.get("isListed") is not False]


def smartrecruiters(slug: str, fetch: Fetch) -> list[dict] | None:
    data = fetch(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    # SmartRecruiters answers 200 with nothing for unknown companies, so an empty list means "no board".
    if not isinstance(data, dict) or not data.get("content"):
        return None
    return [{
        "board": "smartrecruiters",
        "title": j.get("name", ""),
        "location": (j.get("location") or {}).get("fullLocation", ""),
        "date": to_date(j.get("releasedDate")),
        "source": f"https://jobs.smartrecruiters.com/{slug}/{j.get('id', '')}",
    } for j in data["content"]]


def recruitee(slug: str, fetch: Fetch) -> list[dict] | None:
    data = fetch(f"https://{slug}.recruitee.com/api/offers/")
    if not isinstance(data, dict) or "offers" not in data:
        return None
    return [{
        "board": "recruitee",
        "title": j.get("title", ""),
        "location": j.get("location", ""),
        "date": to_date(j.get("published_at")),
        "source": j.get("careers_url", ""),
    } for j in data["offers"]]


def breezy(slug: str, fetch: Fetch) -> list[dict] | None:
    data = fetch(f"https://{slug}.breezy.hr/json")
    if not isinstance(data, list):
        return None
    return [{
        "board": "breezy",
        "title": j.get("name", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "date": to_date(j.get("published_date")),
        "source": j.get("url", ""),
    } for j in data]


def personio(slug: str, fetch: Fetch) -> list[dict] | None:
    data = fetch(f"https://{slug}.jobs.personio.de/xml")
    if not isinstance(data, str) or "<workzag-jobs" not in data:
        return None
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return None
    out = []
    for pos in root.findall("position"):
        pid = (pos.findtext("id") or "").strip()
        out.append({
            "board": "personio",
            "title": (pos.findtext("name") or "").strip(),
            "location": (pos.findtext("office") or "").strip(),
            "date": to_date((pos.findtext("createdAt") or "").strip()),
            "source": f"https://{slug}.jobs.personio.de/job/{pid}" if pid else "",
        })
    return out


BOARDS = (greenhouse, lever, ashby, smartrecruiters, recruitee, breezy, personio)


def find_roles(company: str, fetch: Fetch = http_json, keywords: Iterable[str] = (), days: int | None = None,
               today: dt.date | None = None) -> tuple[list[dict], list[str]]:
    """Return (roles, notes). Roles are newest first and say which board name matched; notes say
    which boards were found or could not be reached."""
    today = today or dt.date.today()
    kws = [k.strip().lower() for k in keywords if k and k.strip()]
    roles, notes = [], []
    for board in BOARDS:
        for slug in slugs(company):
            try:
                found = board(slug, fetch)
            except NETWORK_ERRORS as exc:
                notes.append(f"{board.__name__}/{slug}: could not reach ({exc})")
                break
            if found is not None:
                notes.append(f"{board.__name__}/{slug}: {len(found)} open roles")
                for r in found:
                    r["company"] = company
                    r["matched"] = f"{board.__name__}/{slug}"
                roles.extend(found)
                break
    if kws:
        roles = [r for r in roles if any(k in r["title"].lower() for k in kws)]
    if days is not None:
        roles = [r for r in roles if r["date"] and (today - dt.date.fromisoformat(r["date"])).days <= days]
    roles.sort(key=lambda r: r["date"] or "", reverse=True)
    return roles, notes


def as_signal(role: dict, signal_id: str) -> dict:
    where = f" ({role['location']})" if role.get("location") else ""
    return {"id": signal_id, "date": role["date"], "detail": f"Hiring: {role['title']}{where}", "source": role["source"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("companies", nargs="+", help="company names or domains (about 10 per call)")
    ap.add_argument("--keywords", default="", help="comma-separated words to match in job titles")
    ap.add_argument("--days", type=int, help="only roles posted in the last N days")
    ap.add_argument("--json", action="store_true", help="print one JSON object per role")
    ap.add_argument("--signal-id", help="also print each role as a leads.json signal with this id")
    args = ap.parse_args(argv)
    kws = args.keywords.split(",") if args.keywords else []

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda c: (c, *find_roles(c, keywords=kws, days=args.days)), args.companies))

    reached_any = False
    for company, roles, notes in results:
        reached_any = reached_any or any("could not reach" not in n for n in notes) or not notes
        for n in notes:
            print(f"{company}: {n}", file=sys.stderr)
        if not notes:
            print(f"{company}: no board on Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee, Breezy or "
                  f"Personio (unknown, not 'not hiring')", file=sys.stderr)
        for r in roles:
            if args.signal_id:
                print(json.dumps({"company": company, "matched": r["matched"], **as_signal(r, args.signal_id)}))
            elif args.json:
                print(json.dumps({k: r[k] for k in ("company", "matched", "board", "title", "location", "date", "source")}))
            else:
                print(f"{r['date'] or '????-??-??'}  {company}  {r['title']}  [{r['matched']}]  {r['source']}")
        sys.stdout.flush()
    if not reached_any:
        print("no job board could be reached: hiring is unknown for every company", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
