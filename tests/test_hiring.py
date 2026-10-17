import datetime as dt
import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "find-leads" / "scripts"))
import hiring  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
TODAY = dt.date(2026, 10, 17)


def fake_fetch(routes):
    """Return a fetch function that serves fixtures by URL and None (404) for anything else."""
    calls = []

    def fetch(url):
        calls.append(url)
        if url in routes:
            value = routes[url]
            if isinstance(value, Exception):
                raise value
            text = (FIX / value).read_text()
            return text if value.endswith(".xml") else json.loads(text)
        return None

    fetch.calls = calls
    return fetch


GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
LV = "https://api.lever.co/v0/postings/{}?mode=json"
AB = "https://api.ashbyhq.com/posting-api/job-board/{}"
SR = "https://api.smartrecruiters.com/v1/companies/{}/postings?limit=100"
RC = "https://{}.recruitee.com/api/offers/"
BZ = "https://{}.breezy.hr/json"
PS = "https://{}.jobs.personio.de/xml"


class SlugTests(unittest.TestCase):
    def test_names_and_domains(self):
        self.assertEqual(hiring.slugs("Acme Labs"), ["acmelabs", "acme-labs", "acme"])
        self.assertEqual(hiring.slugs("https://www.acme.io/careers"), ["acme"])
        self.assertEqual(hiring.slugs("linear.app"), ["linear"])
        self.assertEqual(hiring.slugs("Acme, Inc."), ["acmeinc", "acme-inc", "acme"])
        self.assertEqual(hiring.slugs("  "), [])

    def test_names_with_dots_are_not_domains(self):
        self.assertEqual(hiring.slugs("J.P. Morgan"), ["jpmorgan", "j-p-morgan"])
        self.assertEqual(hiring.slugs("St. James Software"), ["stjamessoftware", "st-james-software", "stjames", "st-james"])
        self.assertEqual(hiring.slugs("Dr. Reddy's Laboratories")[0], "drreddyslaboratories")

    def test_never_falls_back_to_first_word(self):
        self.assertNotIn("the", hiring.slugs("The Trade Desk"))
        self.assertNotIn("fission", hiring.slugs("Fission Gaming"))

    def test_accents(self):
        self.assertEqual(hiring.slugs("Zühlke Engineering")[0], "zuhlkeengineering")


class DateTests(unittest.TestCase):
    def test_invalid_dates_are_none(self):
        self.assertIsNone(hiring.to_date("0000-00-00 00:00:00"))
        self.assertIsNone(hiring.to_date("2026-13-40"))

    def test_formats(self):
        self.assertEqual(hiring.to_date("2026-10-02T10:00:00-04:00"), "2026-10-02")
        self.assertEqual(hiring.to_date("2026-10-02T10:00:00.000Z"), "2026-10-02")
        self.assertEqual(hiring.to_date(1790000000000), "2026-09-21")
        self.assertIsNone(hiring.to_date(""))
        self.assertIsNone(hiring.to_date("soon"))


class BoardTests(unittest.TestCase):
    def test_greenhouse(self):
        fetch = fake_fetch({GH.format("acme"): "greenhouse.json"})
        roles, notes = hiring.find_roles("Acme", fetch=fetch, today=TODAY)
        self.assertEqual([r["title"] for r in roles], ["Business Development Manager, US", "Senior Backend Engineer"])
        self.assertEqual(roles[0]["date"], "2026-10-02")
        self.assertIsNone(roles[1]["date"])        # only updated_at: an edit is not a posting date
        self.assertEqual(roles[0]["matched"], "greenhouse/acme")
        self.assertTrue(roles[0]["source"].startswith("https://"))
        self.assertIn("greenhouse/acme: 2 open roles", notes)

    def test_lever(self):
        fetch = fake_fetch({LV.format("acme"): "lever.json"})
        roles, _ = hiring.find_roles("acme", fetch=fetch, today=TODAY)
        self.assertEqual(roles[0]["board"], "lever")
        self.assertEqual(roles[0]["title"], "Partnerships Lead")
        self.assertEqual(roles[0]["location"], "London")

    def test_ashby_skips_unlisted(self):
        fetch = fake_fetch({AB.format("acme"): "ashby.json"})
        roles, _ = hiring.find_roles("acme", fetch=fetch, today=TODAY)
        self.assertEqual([r["title"] for r in roles], ["Account Executive"])

    def test_keyword_and_days_filters(self):
        fetch = fake_fetch({GH.format("acme"): "greenhouse.json", LV.format("acme"): "lever.json"})
        roles, _ = hiring.find_roles("acme", fetch=fetch, keywords=["business development", "partnerships"], days=30, today=TODAY)
        self.assertEqual([r["title"] for r in roles], ["Business Development Manager, US"])

    def test_tries_slug_variants(self):
        fetch = fake_fetch({GH.format("acme-labs"): "greenhouse.json"})
        roles, notes = hiring.find_roles("Acme Labs", fetch=fetch, today=TODAY)
        self.assertEqual(len(roles), 2)
        self.assertIn(GH.format("acmelabs"), fetch.calls)
        self.assertIn("greenhouse/acme-labs: 2 open roles", notes)

    def test_empty_board_is_reported(self):
        fetch = fake_fetch({GH.format("acme"): "greenhouse_empty.json"})
        roles, notes = hiring.find_roles("acme", fetch=fetch, today=TODAY)
        self.assertEqual(roles, [])
        self.assertIn("greenhouse/acme: 0 open roles", notes)

    def test_no_board_found(self):
        roles, notes = hiring.find_roles("nobody", fetch=fake_fetch({}), today=TODAY)
        self.assertEqual((roles, notes), ([], []))

    def test_incomplete_read_is_a_network_error(self):
        import http.client
        fetch = fake_fetch({GH.format("acme"): http.client.IncompleteRead(b"")})
        roles, notes = hiring.find_roles("acme", fetch=fetch, today=TODAY)
        self.assertTrue(any("could not reach" in n for n in notes))

    def test_as_signal(self):
        role = {"title": "BD Manager", "location": "New York", "date": "2026-10-02", "source": "https://x.test/1"}
        self.assertEqual(hiring.as_signal(role, "hiring_bd"),
                         {"id": "hiring_bd", "date": "2026-10-02", "detail": "Hiring: BD Manager (New York)", "source": "https://x.test/1"})

    def test_network_error_is_a_note_not_a_crash(self):
        fetch = fake_fetch({GH.format("acme"): urllib.error.URLError("offline"), LV.format("acme"): "lever.json"})
        roles, notes = hiring.find_roles("acme", fetch=fetch, today=TODAY)
        self.assertTrue(any("could not reach" in n for n in notes))
        self.assertEqual(roles[0]["board"], "lever")



class CliTests(unittest.TestCase):
    def test_exit_3_when_every_board_is_unreachable(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock
        def boom(url):
            raise urllib.error.URLError("blocked")
        with mock.patch.object(hiring, "http_json", boom), mock.patch.object(hiring.find_roles, "__defaults__", (boom, (), None, None)):
            err, out = io.StringIO(), io.StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                code = hiring.main(["acme", "globex"])
        self.assertEqual(code, 3)
        self.assertIn("hiring is unknown", err.getvalue())

    def test_signal_id_output(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock
        fetch = fake_fetch({GH.format("acme"): "greenhouse.json"})
        with mock.patch.object(hiring.find_roles, "__defaults__", (fetch, (), None, None)):
            out = io.StringIO()
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                code = hiring.main(["acme", "--keywords", "business", "--signal-id", "hiring_bd"])
        self.assertEqual(code, 0)
        line = json.loads(out.getvalue().splitlines()[0])
        self.assertEqual((line["id"], line["date"], line["matched"]), ("hiring_bd", "2026-10-02", "greenhouse/acme"))
        self.assertEqual(line["detail"], "Hiring: Business Development Manager, US (New York)")


class RecordedLiveResponses(unittest.TestCase):
    """Real responses recorded from each board on 2026-10-17 (trimmed to the fields the script reads)."""

    def test_greenhouse_gitlab(self):
        fetch = fake_fetch({GH.format("gitlab"): "live_greenhouse_gitlab.json"})
        roles, _ = hiring.find_roles("GitLab", fetch=fetch, keywords=["account executive"], days=30, today=TODAY)
        self.assertEqual(len(roles), 1)
        self.assertEqual(roles[0]["date"], "2026-10-02")          # first_published, not updated_at
        self.assertEqual(roles[0]["location"], "Remote, France")
        self.assertEqual(roles[0]["source"], "https://job-boards.greenhouse.io/gitlab/jobs/8860302002")

    def test_lever_leverdemo(self):
        fetch = fake_fetch({LV.format("leverdemo"): "live_lever_leverdemo.json"})
        roles, _ = hiring.find_roles("leverdemo", fetch=fetch, today=TODAY)
        self.assertEqual(roles[0]["title"], "Customer Success Manager")
        self.assertEqual(roles[0]["date"], "2026-08-31")
        self.assertEqual(roles[-1]["date"], "2024-06-17")

    def test_ashby_linear(self):
        fetch = fake_fetch({AB.format("linear"): "live_ashby_linear.json"})
        roles, _ = hiring.find_roles("linear.app", fetch=fetch, today=TODAY)
        self.assertEqual(len(roles), 3)
        self.assertEqual(roles[0]["date"], "2022-01-22")
        roles_recent, _ = hiring.find_roles("linear.app", fetch=fetch, days=180, today=TODAY)
        self.assertEqual(roles_recent, [])


    def test_smartrecruiters_boschgroup(self):
        fetch = fake_fetch({SR.format("boschgroup"): "live_smartrecruiters_boschgroup.json"})
        roles, notes = hiring.find_roles("BoschGroup", fetch=fetch, today=TODAY)
        self.assertEqual(roles[0]["title"], "Janitor (conserje)")
        self.assertEqual(roles[0]["date"], "2026-10-17")
        self.assertEqual(roles[0]["source"], "https://jobs.smartrecruiters.com/boschgroup/744000156094999")
        self.assertIn("smartrecruiters/boschgroup: 2 open roles", notes)

    def test_smartrecruiters_empty_means_no_board(self):
        fetch = fake_fetch({SR.format("acme"): "smartrecruiters_empty.json"})
        roles, notes = hiring.find_roles("acme", fetch=fetch, today=TODAY)
        self.assertEqual((roles, notes), ([], []))

    def test_recruitee_bunq(self):
        fetch = fake_fetch({RC.format("bunq"): "live_recruitee_bunq.json"})
        roles, _ = hiring.find_roles("bunq", fetch=fetch, keywords=["product"], days=7, today=TODAY)
        self.assertEqual([r["date"] for r in roles], ["2026-10-15", "2026-10-13"])
        self.assertEqual(roles[0]["source"], "https://careers.bunq.com/o/head-of-product-6")

    def test_breezy(self):
        fetch = fake_fetch({BZ.format("breezy"): "live_breezy_breezy.json"})
        roles, _ = hiring.find_roles("breezy", fetch=fetch, today=TODAY)
        self.assertEqual(roles[0]["date"], "2024-02-15")
        self.assertEqual(roles[0]["location"], "Chaos, FL")

    def test_personio_xml(self):
        fetch = fake_fetch({PS.format("personio"): "live_personio_personio.xml"})
        roles, _ = hiring.find_roles("personio", fetch=fetch, today=TODAY)
        self.assertEqual(roles, [{"board": "personio", "title": "Staff Software Engineer, Data Platform", "location": "Munich",
                                  "date": "2024-11-13", "source": "https://personio.jobs.personio.de/job/1834171", "company": "personio",
                                  "matched": "personio/personio"}])

    def test_personio_ignores_html(self):
        self.assertIsNone(hiring.personio("acme", lambda url: "<!DOCTYPE html><html></html>"))


if __name__ == "__main__":
    unittest.main()
