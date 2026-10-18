import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "check-receipts" / "scripts"))
import receipts  # noqa: E402

AS_OF = dt.date(2026, 10, 19)
SRC = "https://example.com/careers"


def claim(text, **kw):
    return {"text": text, "kind": "claim", "about": "prospect", **kw}


class ReceiptsTest(unittest.TestCase):
    def run_items(self, items, **extra):
        return receipts.score({"items": items, **extra}, AS_OF)

    def test_sourced_claim_needs_link_and_quote(self):
        r, w = self.run_items([claim("You have 6 open SDR roles.", source=SRC, quote="SDR (6)", seen="2026-10-19")])
        self.assertEqual((r["sourced"], r["claims"], r["score"]), (1, 1, 100))
        self.assertEqual(w, [])

    def test_missing_quote_is_unsourced_with_warning(self):
        r, w = self.run_items([claim("You have 6 open SDR roles.", source=SRC)])
        self.assertEqual(r["sourced"], 0)
        self.assertEqual(r["items"][0]["status"], "unsourced")
        self.assertIn("no quote", r["items"][0]["note"])
        self.assertTrue(any("receipt dropped" in x for x in w))

    def test_non_http_source_is_rejected(self):
        r, _ = self.run_items([claim("You raised $12M.", source="press release", quote="raised $12M")])
        self.assertEqual(r["items"][0]["status"], "unsourced")
        self.assertIn("no public link", r["items"][0]["note"])

    def test_future_date_is_rejected(self):
        r, _ = self.run_items([claim("You raised $12M.", source=SRC, quote="raised $12M", date="2027-01-01")])
        self.assertEqual(r["items"][0]["status"], "unsourced")

    def test_bad_date_is_rejected_not_crash(self):
        r, _ = self.run_items([claim("You raised $12M.", source=SRC, quote="raised", date="last May")])
        self.assertEqual(r["items"][0]["status"], "unsourced")
        self.assertIn("YYYY-MM-DD", r["items"][0]["note"])

    def test_signal_needs_date(self):
        r, _ = self.run_items([claim("You're hiring SDRs.", source=SRC, quote="SDR", signal=True)])
        self.assertEqual(r["items"][0]["status"], "unsourced")
        self.assertIn("R5", r["items"][0]["note"])

    def test_stale_signal_is_sourced_but_flagged(self):
        r, w = self.run_items([claim("You raised a Series A.", source=SRC, quote="Series A", date="2025-01-10", signal=True)])
        self.assertEqual(r["items"][0]["status"], "sourced")
        self.assertTrue(r["items"][0]["stale"])
        self.assertEqual(r["stale"], 1)
        self.assertTrue(any("stale" in x for x in w))

    def test_window_is_configurable(self):
        r, _ = self.run_items([claim("You raised.", source=SRC, quote="raised", date="2026-09-01", signal=True)], window_days=30)
        self.assertTrue(r["items"][0].get("stale"))

    def test_guess_counts_as_claim_never_sourced(self):
        r, _ = self.run_items([
            claim("You have 6 roles.", source=SRC, quote="6"),
            {"text": "Teams like yours struggle.", "kind": "guess", "source": SRC, "quote": "x"},
        ])
        self.assertEqual((r["sourced"], r["claims"], r["guesses"], r["score"]), (1, 2, 1, 50))

    def test_questions_and_plain_are_not_claims(self):
        r, _ = self.run_items([
            {"text": "How are you ramping reps?", "kind": "asked"},
            {"text": "Hi Priya,", "kind": "plain"},
        ])
        self.assertEqual((r["claims"], r["asked"], r["score"]), (0, 1, 100))

    def test_unknown_kind_treated_as_claim(self):
        r, w = self.run_items([{"text": "We're #1.", "kind": "boast"}])
        self.assertEqual(r["claims"], 1)
        self.assertTrue(any("unknown kind" in x for x in w))

    def test_summary_line(self):
        r, _ = self.run_items([claim("a", source=SRC, quote="a"), claim("b"), claim("c")])
        self.assertEqual(r["summary"], "Receipts: 1 of 3 claims (33)")

    def test_cli_writes_markdown(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "receipts.json"
            p.write_text(json.dumps({"as_of": "2026-10-19", "items": [
                claim("You have 6 open SDR roles.", source=SRC, quote="SDR (6)", date="2026-10-02", signal=True),
                {"text": "Teams like yours | struggle.", "kind": "guess"},
            ]}), encoding="utf-8")
            self.assertEqual(receipts.main([str(p)]), 0)
            md = (Path(d) / "receipts.md").read_text(encoding="utf-8")
            self.assertIn("# Receipts: 1 of 2 claims (50)", md)
            self.assertIn(f"[{SRC}]({SRC})", md)
            self.assertIn("Teams like yours \\| struggle.", md)
            self.assertNotIn("machgtm.com/check", md)

    def test_cli_bad_as_of(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.json"
            p.write_text("{}", encoding="utf-8")
            self.assertEqual(receipts.main([str(p), "--as-of", "yesterday"]), 2)


if __name__ == "__main__":
    unittest.main()
