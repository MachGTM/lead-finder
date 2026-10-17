#!/usr/bin/env python3
"""Score and rank leads with a fixed formula. The model gathers evidence; this script does the maths.

Usage:
    python3 score.py leads.json [--out-dir DIR] [--as-of YYYY-MM-DD]

Reads a leads file (see reference/scorecard.md), checks that every claim has a source,
scores each company, and writes leads.csv and leads.md next to the input (or into --out-dir).
Prints the ranked table and any warnings. Standard library only.

Formula
    fit       = met weight / known weight, as 0-100. Unknown signs count neither way.
    coverage  = known weight / total weight, as 0-100. Below 50 the fit score is marked "thin".
    intent    = sum over signal types of (weight x recency of its freshest dated, sourced copy)
                / total signal weight, as 0-100. Five job posts count as one hiring signal.
                recency: 0-30 days 1.0, 31-90 days 0.6, 91-180 days 0.3, older 0.
    priority  = Call now   fit >= 60 and intent >= 30
                Nurture    fit >= 60 and intent < 30
                Qualify    fit < 60 and intent >= 30
                Skip       otherwise
                When coverage is below 50 the fit is unproven, so the lead can only be Qualify or Skip.
                A sign marked "required": true that is not_met makes the lead Skip, whatever the scores.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

FIT_BAR = 60
INTENT_BAR = 30
COVERAGE_BAR = 50
RECENCY = ((30, 1.0), (90, 0.6), (180, 0.3))
STATES = {"met", "not_met", "unknown"}
PRIORITY_ORDER = {"Call now": 0, "Qualify": 1, "Nurture": 2, "Skip": 3}


class LeadsError(ValueError):
    """The leads file is not usable at all."""


def parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def recency(days: int) -> float:
    if days < 0:
        return 0.0
    for limit, factor in RECENCY:
        if days <= limit:
            return factor
    return 0.0


def host(domain) -> str:
    """'https://www.Acme.com/about' -> 'acme.com'."""
    text = str(domain or "").strip().lower()
    text = text.split("://", 1)[-1].split("/", 1)[0]
    return text.removeprefix("www.")


def has_source(item: dict) -> bool:
    src = str(item.get("source") or "").strip()
    return src.startswith("http://") or src.startswith("https://")


def load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LeadsError(f"{path} not found") from exc
    except json.JSONDecodeError as exc:
        raise LeadsError(f"{path} is not valid JSON: {exc}") from exc
    icp = data.get("icp") or {}
    if not icp.get("signs"):
        raise LeadsError("icp.signs is empty: define at least one fit sign with an id and a weight")
    if not isinstance(data.get("leads"), list):
        raise LeadsError("leads must be a list")
    for i, lead in enumerate(data["leads"]):
        if not isinstance(lead, dict):
            raise LeadsError(f"leads[{i}] must be an object with a company, signs and signals")
        for key in ("signs", "signals"):
            items = lead.get(key) or []
            if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
                raise LeadsError(f"leads[{i}].{key} must be a list of objects with an id")
    return data


def weights(defs: list[dict], kind: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in defs or []:
        key = str(d.get("id") or "").strip()
        if not key:
            raise LeadsError(f"every {kind} needs an id")
        w = d.get("weight", 1)
        if not isinstance(w, (int, float)) or w <= 0:
            raise LeadsError(f"{kind} {key!r} needs a positive weight")
        out[key] = {"label": d.get("label") or key, "weight": float(w), "required": bool(d.get("required"))}
    return out


def score_lead(lead: dict, signs: dict, signals: dict, as_of: dt.date, warnings: list[str]) -> dict:
    name = lead.get("company") or "(unnamed)"
    total_w = sum(s["weight"] for s in signs.values())
    met_w = known_w = 0.0
    met, missed, unknown, failed_must = [], [], [], []
    seen = {}
    for item in lead.get("signs") or []:
        sid = item.get("id")
        if sid not in signs:
            warnings.append(f"{name}: sign {sid!r} is not in the scorecard; ignored")
            continue
        state = item.get("state", "unknown")
        if state not in STATES:
            warnings.append(f"{name}: sign {sid!r} has state {state!r}; treated as unknown")
            state = "unknown"
        if state in ("met", "not_met") and not has_source(item):
            warnings.append(f"{name}: sign {sid!r} is {state} but has no source URL; treated as unknown")
            state = "unknown"
        if sid in seen:
            warnings.append(f"{name}: sign {sid!r} appears twice; kept the entry with evidence")
            if state == "unknown" or seen[sid][0] != "unknown":
                continue
        seen[sid] = (state, item)
    for sid, spec in signs.items():
        state, item = seen.get(sid, ("unknown", {}))
        label = spec["label"]
        if state == "met":
            met_w += spec["weight"]
            known_w += spec["weight"]
            met.append((label, item.get("source", ""), item.get("quote", "")))
        elif state == "not_met":
            known_w += spec["weight"]
            missed.append((label, item.get("source", ""), item.get("quote", "")))
            if spec["required"]:
                failed_must.append(label)
        else:
            unknown.append(label)
    fit = round(100 * met_w / known_w) if known_w else 0
    coverage = round(100 * known_w / total_w) if total_w else 0

    total_sig = sum(s["weight"] for s in signals.values())
    best: dict[str, float] = {}
    why_now = []
    for sig in lead.get("signals") or []:
        sid = sig.get("id")
        if sid not in signals:
            warnings.append(f"{name}: signal {sid!r} is not in the scorecard; ignored")
            continue
        if not has_source(sig):
            warnings.append(f"{name}: signal {sid!r} has no source URL; dropped")
            continue
        when = parse_date(sig.get("date"))
        if when is None:
            warnings.append(f"{name}: signal {sid!r} has no valid date (YYYY-MM-DD); dropped")
            continue
        age = (as_of - when).days
        if age < 0:
            warnings.append(f"{name}: signal {sid!r} is dated in the future ({when}); dropped")
            continue
        factor = recency(age)
        best[sid] = max(best.get(sid, 0.0), factor)
        why_now.append({
            "label": signals[sid]["label"],
            "detail": sig.get("detail", ""),
            "date": when.isoformat(),
            "age_days": age,
            "counts": factor > 0,
            "source": sig["source"],
        })
    points = sum(signals[sid]["weight"] * factor for sid, factor in best.items())
    intent = min(100, round(100 * points / total_sig)) if total_sig else 0
    why_now.sort(key=lambda s: s["age_days"])

    thin = coverage < COVERAGE_BAR
    if failed_must:
        priority = "Skip"
    elif fit >= FIT_BAR and not thin:
        priority = "Call now" if intent >= INTENT_BAR else "Nurture"
    elif intent >= INTENT_BAR:
        priority = "Qualify"
    else:
        priority = "Skip"

    return {
        "company": name,
        "domain": host(lead.get("domain")),
        "summary": lead.get("summary", ""),
        "fit": fit,
        "coverage": coverage,
        "thin": thin,
        "intent": intent,
        "priority": priority,
        "met": met,
        "missed": missed,
        "unknown": unknown,
        "failed_must": failed_must,
        "why_now": why_now,
        "questions": [q for q in (lead.get("questions") or []) if str(q).strip()],
        "first_question": lead.get("first_question", ""),
    }


def rank(scored: list[dict]) -> list[dict]:
    return sorted(scored, key=lambda r: (PRIORITY_ORDER[r["priority"]], -r["fit"], -r["intent"], -r["coverage"], r["company"].lower()))


def dedupe(leads: list[dict], warnings: list[str]) -> list[dict]:
    out, seen = [], set()
    for lead in leads:
        key = host(lead.get("domain")) or str(lead.get("company") or "").strip().lower()
        if key in seen:
            warnings.append(f"duplicate lead {lead.get('company')!r} ({key}); kept the first")
            continue
        seen.add(key)
        out.append(lead)
    return out


def write_csv(rows: list[dict], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["rank", "company", "domain", "priority", "fit", "coverage", "intent", "why_fit", "why_now", "unknowns", "fails_must_have", "first_question", "sources"])
        for i, r in enumerate(rows, 1):
            sources = sorted({s for _, s, _ in r["met"] + r["missed"] if s} | {s["source"] for s in r["why_now"]})
            w.writerow([
                i, r["company"], r["domain"], r["priority"], r["fit"], r["coverage"], r["intent"],
                "; ".join(label for label, _, _ in r["met"]),
                "; ".join(f"{s['label']}: {s['detail']} ({s['date']})" for s in r["why_now"] if s["counts"]),
                "; ".join(r["unknown"]),
                "; ".join(r["failed_must"]),
                r["first_question"],
                " ".join(sources),
            ])


def md_escape(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def write_md(rows: list[dict], data: dict, as_of: dt.date, path: Path) -> None:
    icp = data.get("icp") or {}
    lines = [f"# Leads: {md_escape(icp.get('summary') or 'your ideal customer')}", "",
             f"As of {as_of.isoformat()}. Scores come from a fixed formula; every claim links to where it was found.", "",
             "| # | Company | Priority | Fit | Intent | Why now |", "|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        now = next((s for s in r["why_now"] if s["counts"]), None)
        now_txt = f"{md_escape(now['detail'] or now['label'])} ({now['date']})" if now else "No recent signal"
        fit_txt = f"{r['fit']}{' (thin)' if r['thin'] else ''}"
        name = f"[{md_escape(r['company'])}](https://{r['domain']})" if r["domain"] else md_escape(r["company"])
        lines.append(f"| {i} | {name} | {r['priority']} | {fit_txt} | {r['intent']} | {now_txt} |")
    lines.append("")
    for i, r in enumerate(rows, 1):
        lines += [f"## {i}. {md_escape(r['company'])}: {r['priority']}", ""]
        if r["summary"]:
            lines += [md_escape(r["summary"]), ""]
        lines.append(f"Fit {r['fit']} (known for {r['coverage']}% of the scorecard) · Intent {r['intent']}")
        lines.append("")
        for label in r["failed_must"]:
            lines.append(f"- **Fails a must-have:** {md_escape(label)}")
        for label, src, quote in r["met"]:
            q = f": \"{md_escape(quote)}\"" if quote else ""
            lines.append(f"- Fits: {md_escape(label)}{q} ([source]({src}))")
        for label, src, quote in r["missed"]:
            q = f": \"{md_escape(quote)}\"" if quote else ""
            lines.append(f"- Doesn't fit: {md_escape(label)}{q} ([source]({src}))")
        for s in r["why_now"]:
            stale = "" if s["counts"] else " (too old to count)"
            lines.append(f"- Why now: {md_escape(s['detail'] or s['label'])}, {s['date']}{stale} ([source]({s['source']}))")
        for label in r["unknown"]:
            lines.append(f"- Unknown: {md_escape(label)}")
        for q in r["questions"]:
            lines.append(f"- Open question: {md_escape(q)}")
        if r["first_question"]:
            lines += ["", f"A first question to ask: {md_escape(r['first_question'])}"]
        lines.append("")
    lines.append("Made with [Lead Finder](https://machgtm.com) by MachGTM. Every claim above links to where it was found.")
    path.write_text("\n".join(lines), encoding="utf-8")


def run(path: Path, out_dir: Path | None = None, as_of: dt.date | None = None) -> tuple[list[dict], list[str]]:
    data = load(path)
    signs = weights(data["icp"].get("signs"), "sign")
    signals = weights(data["icp"].get("signals"), "signal")
    as_of = as_of or parse_date(data.get("as_of")) or dt.date.today()
    warnings: list[str] = []
    leads = dedupe(data["leads"], warnings)
    rows = rank([score_lead(lead, signs, signals, as_of, warnings) for lead in leads])
    out_dir = out_dir or path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(rows, out_dir / "leads.csv")
    write_md(rows, data, as_of, out_dir / "leads.md")
    return rows, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("leads", type=Path)
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--as-of", help="score as of this date (YYYY-MM-DD); default: the file's as_of, else today")
    args = ap.parse_args(argv)
    as_of = parse_date(args.as_of) if args.as_of else None
    if args.as_of and as_of is None:
        print("error: --as-of must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        rows, warnings = run(args.leads, args.out_dir, as_of)
    except LeadsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{'#':>2}  {'priority':<9} {'fit':>4} {'cov':>4} {'int':>4}  company")
    for i, r in enumerate(rows, 1):
        note = f"  (fails a must-have: {'; '.join(r['failed_must'])})" if r["failed_must"] else ""
        print(f"{i:>2}  {r['priority']:<9} {r['fit']:>4} {r['coverage']:>4} {r['intent']:>4}  {r['company']}{note}")
    out = args.out_dir or args.leads.parent
    print(f"\nwrote {out / 'leads.csv'} and {out / 'leads.md'}")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
