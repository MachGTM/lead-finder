#!/usr/bin/env python3
"""Score a sales message against the Receipts Standard v1. The model finds claims and sources; this script does the maths.

Usage:
    python3 receipts.py receipts.json [--out-dir DIR] [--as-of YYYY-MM-DD]

Reads a receipts file (see SKILL.md), checks every receipt against the standard,
and writes receipts.md next to the input (or into --out-dir). Prints the score line
and any warnings. Standard library only.

Rules
    A claim counts as sourced only if its receipt has an http(s) source and a quote.
    A receipt dated in the future, or with a date that isn't YYYY-MM-DD, is dropped.
    A signal (a reason to reach out now) needs a date; older than the window it is stale.
    Guesses never count as sourced. Questions and plain lines are not claims.

Score
    score = sourced claims / all claims (claims + guesses) x 100, rounded.
    A message with no claims scores 100: it asserts nothing it can't back.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

KINDS = {"claim", "guess", "asked", "plain"}
ABOUT = {"prospect", "seller", "world"}
DEFAULT_WINDOW = 180


def _date(value):
    if value in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise ValueError(f"not a YYYY-MM-DD date: {value!r}")


def check_item(item: dict, as_of: dt.date, window: int, warnings: list[str]) -> dict:
    text = str(item.get("text", "")).strip()
    kind = item.get("kind", "claim")
    if kind not in KINDS:
        warnings.append(f"unknown kind {kind!r} for {text[:50]!r}; treated as a claim")
        kind = "claim"
    out = {"text": text, "kind": kind, "about": item.get("about"), "status": kind, "note": ""}
    if kind in ("asked", "plain"):
        return out
    if kind == "guess":
        out["status"] = "guess"
        out["note"] = "Guess stated as fact. Ask it as a question (R3)."
        return out

    if out["about"] not in ABOUT:
        out["about"] = "prospect"
    source = str(item.get("source") or "").strip()
    quote = str(item.get("quote") or "").strip()
    problems = []
    if not source.lower().startswith(("http://", "https://")):
        problems.append("no public link")
    if not quote:
        problems.append("no quote from the page")
    try:
        when = _date(item.get("date"))
        seen = _date(item.get("seen"))
    except ValueError as exc:
        problems.append(str(exc))
        when = seen = None
    if when and when > as_of:
        problems.append(f"dated in the future ({when})")
    if seen and seen > as_of:
        problems.append(f"seen in the future ({seen})")
    if item.get("signal") and not when:
        problems.append("a signal needs the date it happened (R5)")

    if problems:
        out["status"] = "unsourced"
        out["note"] = "; ".join(problems)
        if source or quote:
            warnings.append(f"receipt dropped for {text[:50]!r}: {out['note']}")
        return out

    out.update(status="sourced", source=source, quote=quote, date=str(when) if when else "", seen=str(seen) if seen else "")
    if item.get("signal") and when and (as_of - when).days > window:
        out["stale"] = True
        out["note"] = f"{(as_of - when).days} days old: past the {window}-day window, so don't call it recent (R5)."
        warnings.append(f"stale signal for {text[:50]!r}: {when}")
    return out


def score(data: dict, as_of: dt.date | None = None) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    as_of = as_of or _date(data.get("as_of")) or dt.date.today()
    window = int(data.get("window_days") or DEFAULT_WINDOW)
    items = [check_item(i, as_of, window, warnings) for i in data.get("items", []) if isinstance(i, dict)]
    claims = [i for i in items if i["status"] in ("sourced", "unsourced", "guess")]
    sourced = sum(1 for i in claims if i["status"] == "sourced")
    total = len(claims)
    value = 100 if total == 0 else round(100 * sourced / total)
    result = {
        "as_of": str(as_of),
        "window_days": window,
        "items": items,
        "claims": total,
        "sourced": sourced,
        "unsourced": sum(1 for i in claims if i["status"] == "unsourced"),
        "guesses": sum(1 for i in claims if i["status"] == "guess"),
        "asked": sum(1 for i in items if i["status"] == "asked"),
        "stale": sum(1 for i in items if i.get("stale")),
        "score": value,
        "summary": f"Receipts: {sourced} of {total} claims ({value})",
    }
    return result, warnings


def to_markdown(r: dict) -> str:
    lines = [f"# {r['summary']}", "", f"Checked against the Receipts Standard v1.0 on {r['as_of']}. https://github.com/MachGTM/receipts/blob/main/STANDARD.md", ""]
    lines += ["| # | Line | Status | Source |", "|---|---|---|---|"]
    n = 0
    labels = {"sourced": "Sourced", "unsourced": "No source", "guess": "Guess", "asked": "Question", "plain": "Not a claim"}
    for it in r["items"]:
        if it["status"] == "plain":
            continue
        n += 1
        text = it["text"].replace("|", "\\|")
        status = labels[it["status"]] + (" (stale)" if it.get("stale") else "")
        if it["status"] == "sourced":
            src = f"[{it['source']}]({it['source']}): \"{it['quote'].replace('|', '/')}\""
            if it.get("date"):
                src += f", {it['date']}"
        else:
            src = it["note"] or ""
        lines.append(f"| {n} | {text} | {status} | {src} |")
    lines += ["", f"Claims: {r['claims']} · sourced: {r['sourced']} · without a source: {r['unsourced']} · guesses: {r['guesses']} · questions: {r['asked']}", ""]
    lines.append("Every sourced claim links to the page it came from.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--out-dir")
    ap.add_argument("--as-of")
    args = ap.parse_args(argv)
    try:
        as_of = _date(args.as_of) if args.as_of else None
    except ValueError:
        print("error: --as-of must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        result, warnings = score(data, as_of)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = Path(args.out_dir) if args.out_dir else Path(args.file).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    (out / "receipts.md").write_text(to_markdown(result), encoding="utf-8")
    print(result["summary"])
    for it in result["items"]:
        if it["status"] != "plain":
            print(f"  {it['status']:<9} {it['text'][:70]}")
    print(f"\nwrote {out / 'receipts.md'}")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
