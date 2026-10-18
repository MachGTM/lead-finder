import csv
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find-leads" / "scripts"))
import score  # noqa: E402

AS_OF = dt.date(2026, 10, 17)
ICP = {
    "summary": "Test buyers",
    "signs": [
        {"id": "a", "label": "Sign A", "weight": 3},
        {"id": "b", "label": "Sign B", "weight": 2},
        {"id": "c", "label": "Sign C", "weight": 1},
    ],
    "signals": [
        {"id": "hire", "label": "Hiring", "weight": 3},
        {"id": "fund", "label": "Funding", "weight": 2},
    ],
}
SRC = "https://example.com/page"


def lead(name, signs=(), signals=(), **extra):
    return {"company": name, "domain": f"{name.lower()}.com", "signs": list(signs), "signals": list(signals), **extra}


def met(i, src=SRC):
    return {"id": i, "state": "met", "source": src, "quote": "q"}


def not_met(i):
    return {"id": i, "state": "not_met", "source": SRC, "quote": "q"}


def sig(i, date, src=SRC):
    return {"id": i, "date": date, "detail": f"{i} detail", "source": src}


class ScoreTests(unittest.TestCase):
    def run_leads(self, leads, icp=ICP):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "leads.json"
        path.write_text(json.dumps({"as_of": AS_OF.isoformat(), "icp": icp, "leads": leads}))
        rows, warnings = score.run(path)
        return rows, warnings, tmp

    def by_name(self, rows):
        return {r["company"]: r for r in rows}

    def test_fit_ignores_unknowns(self):
        rows, _, _ = self.run_leads([lead("X", [met("a"), not_met("b")])])
        r = rows[0]
        self.assertEqual(r["fit"], 60)        # 3 / (3 + 2)
        self.assertEqual(r["coverage"], 83)   # 5 / 6
        self.assertEqual(r["unknown"], ["Sign C"])

    def test_all_met_is_100(self):
        rows, _, _ = self.run_leads([lead("X", [met("a"), met("b"), met("c")])])
        self.assertEqual((rows[0]["fit"], rows[0]["coverage"]), (100, 100))

    def test_nothing_known_scores_zero_and_thin(self):
        rows, _, _ = self.run_leads([lead("X")])
        self.assertEqual((rows[0]["fit"], rows[0]["coverage"], rows[0]["thin"]), (0, 0, True))
        self.assertEqual(rows[0]["priority"], "Skip")

    def test_claim_without_source_becomes_unknown(self):
        rows, warnings, _ = self.run_leads([lead("X", [{"id": "a", "state": "met"}, met("b")])])
        self.assertEqual(rows[0]["fit"], 100)
        self.assertEqual(rows[0]["coverage"], 33)
        self.assertTrue(any("no source URL" in w for w in warnings))

    def test_non_http_source_is_not_a_source(self):
        rows, warnings, _ = self.run_leads([lead("X", [met("a", src="their website")])])
        self.assertEqual(rows[0]["coverage"], 0)
        self.assertTrue(warnings)

    def test_bad_state_is_unknown(self):
        rows, warnings, _ = self.run_leads([lead("X", [{"id": "a", "state": "probably", "source": SRC}])])
        self.assertEqual(rows[0]["coverage"], 0)
        self.assertTrue(any("probably" in w for w in warnings))

    def test_intent_recency(self):
        rows, _, _ = self.run_leads([
            lead("Fresh", signals=[sig("hire", "2026-10-10")]),     # 7 days: 3 * 1.0 / 5
            lead("Month", signals=[sig("hire", "2026-08-01")]),     # 77 days: 3 * 0.6 / 5
            lead("Old", signals=[sig("hire", "2026-05-01")]),       # 169 days: 3 * 0.3 / 5
            lead("Stale", signals=[sig("hire", "2025-10-01")]),     # > 180 days: 0
        ])
        r = self.by_name(rows)
        self.assertEqual(r["Fresh"]["intent"], 60)
        self.assertEqual(r["Month"]["intent"], 36)
        self.assertEqual(r["Old"]["intent"], 18)
        self.assertEqual(r["Stale"]["intent"], 0)
        self.assertFalse(r["Stale"]["why_now"][0]["counts"])

    def test_intent_capped_at_100(self):
        rows, _, _ = self.run_leads([lead("X", signals=[sig("hire", "2026-10-10"), sig("hire", "2026-10-11"), sig("fund", "2026-10-12")])])
        self.assertEqual(rows[0]["intent"], 100)

    def test_each_signal_type_counts_once(self):
        rows, _, _ = self.run_leads([
            lead("FivePosts", signals=[sig("hire", f"2026-10-1{i}") for i in range(5)]),
            lead("OldAndNew", signals=[sig("hire", "2026-05-01"), sig("hire", "2026-10-10")]),
        ])
        r = self.by_name(rows)
        self.assertEqual(r["FivePosts"]["intent"], 60)    # 3 x 1.0 / 5, not 5 x
        self.assertEqual(r["OldAndNew"]["intent"], 60)    # the freshest copy counts
        self.assertEqual(len(r["FivePosts"]["why_now"]), 5)

    def test_domains_are_normalised(self):
        a = dict(lead("Acme", [met("a")]), domain="https://www.Acme.com/about")
        b = dict(lead("Acme again", [met("b")]), domain="acme.com")
        rows, warnings, tmp = self.run_leads([a, b])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["domain"], "acme.com")
        self.assertIn("](https://acme.com)", (tmp / "leads.md").read_text())

    def test_duplicate_sign_keeps_evidence(self):
        rows, warnings, _ = self.run_leads([lead("X", [met("a"), {"id": "a", "state": "unknown"}])])
        self.assertEqual(rows[0]["fit"], 100)
        self.assertTrue(any("appears twice" in w for w in warnings))

    def test_signal_rules(self):
        rows, warnings, _ = self.run_leads([lead("X", signals=[
            {"id": "hire", "date": "2026-10-10", "detail": "no source"},
            {"id": "hire", "date": "last week", "source": SRC},
            {"id": "hire", "date": "2026-12-01", "source": SRC},
            {"id": "nope", "date": "2026-10-10", "source": SRC},
        ])])
        self.assertEqual(rows[0]["intent"], 0)
        self.assertEqual(len(warnings), 4)

    def test_priorities(self):
        strong = [met("a"), met("b"), met("c")]
        weak = [not_met("a"), not_met("b"), met("c")]
        rows, _, _ = self.run_leads([
            lead("CallNow", strong, [sig("hire", "2026-10-10")]),
            lead("Nurture", strong),
            lead("Qualify", weak, [sig("hire", "2026-10-10")]),
            lead("Skip", weak),
            lead("ThinButHot", [met("c")], [sig("hire", "2026-10-10")]),
        ])
        r = self.by_name(rows)
        self.assertEqual(r["CallNow"]["priority"], "Call now")
        self.assertEqual(r["Nurture"]["priority"], "Nurture")
        self.assertEqual(r["Qualify"]["priority"], "Qualify")
        self.assertEqual(r["Skip"]["priority"], "Skip")
        self.assertEqual(r["ThinButHot"]["priority"], "Qualify")   # fit 100 but coverage 17: unproven
        self.assertEqual([x["company"] for x in rows][:2], ["CallNow", "ThinButHot"])

    def test_required_sign_not_met_forces_skip(self):
        icp = dict(ICP, signs=[dict(ICP["signs"][0], required=True)] + ICP["signs"][1:])
        strong_signal = [sig("hire", "2026-10-10")]
        rows, _, tmp = self.run_leads([
            lead("Fails", [not_met("a"), met("b"), met("c")], strong_signal),
            lead("UnknownIsFine", [met("b"), met("c")], strong_signal),
        ], icp=icp)
        r = self.by_name(rows)
        self.assertEqual(r["Fails"]["priority"], "Skip")
        self.assertEqual(r["Fails"]["failed_must"], ["Sign A"])
        self.assertEqual(r["UnknownIsFine"]["priority"], "Call now")   # an unknown must-have never rules a lead out
        self.assertIn("Fails a must-have", (tmp / "leads.md").read_text())

    def test_dedupe_by_domain(self):
        a = lead("Acme", [met("a")])
        b = dict(lead("Acme Inc", [met("b")]), domain="www.acme.com")
        rows, warnings, _ = self.run_leads([a, b])
        self.assertEqual(len(rows), 1)
        self.assertTrue(any("duplicate" in w for w in warnings))

    def test_outputs_written(self):
        rows, _, tmp = self.run_leads([lead("X|Y", [met("a")], [sig("hire", "2026-10-10")], first_question="Is it new?", questions=["How big?"])])
        with (tmp / "leads.csv").open() as fh:
            data = list(csv.DictReader(fh))
        self.assertEqual(data[0]["company"], "X|Y")
        self.assertIn(SRC, data[0]["sources"])
        md = (tmp / "leads.md").read_text()
        self.assertIn("X\\|Y", md)            # pipes escaped in markdown tables
        self.assertIn("Every claim above links to where it was found.", md)
        self.assertNotIn("machgtm.com", md)
        self.assertIn("Open question: How big?", md)
        self.assertIn("A first question to ask: Is it new?", md)

    def test_bad_input_raises(self):
        tmp = Path(tempfile.mkdtemp())
        for content, msg in [
            ("not json", "not valid JSON"),
            (json.dumps({"icp": {"signs": []}, "leads": []}), "icp.signs is empty"),
            (json.dumps({"icp": ICP, "leads": {}}), "leads must be a list"),
            (json.dumps({"icp": {"signs": [{"id": "a", "weight": 0}]}, "leads": []}), "positive weight"),
            (json.dumps({"icp": ICP, "leads": ["Acme"]}), "leads[0] must be an object"),
            (json.dumps({"icp": ICP, "leads": [{"company": "A", "signs": ["a"]}]}), "leads[0].signs must be a list of objects"),
        ]:
            p = tmp / "bad.json"
            p.write_text(content)
            with self.assertRaises(score.LeadsError) as ctx:
                score.run(p)
            self.assertIn(msg, str(ctx.exception))

    def test_cli(self):
        _, _, tmp = self.run_leads([lead("X", [met("a")])])
        self.assertEqual(score.main([str(tmp / "leads.json"), "--as-of", "2026-10-17"]), 0)
        self.assertEqual(score.main([str(tmp / "leads.json"), "--as-of", "yesterday"]), 2)
        self.assertEqual(score.main([str(tmp / "missing.json")]), 2)

    def test_example_file_scores(self):
        example = Path(__file__).resolve().parents[1] / "skills" / "find-leads" / "examples" / "software-services.json"
        out = Path(tempfile.mkdtemp())
        rows, warnings = score.run(example, out_dir=out)
        self.assertEqual((rows, warnings), ([], []))


if __name__ == "__main__":
    unittest.main()
