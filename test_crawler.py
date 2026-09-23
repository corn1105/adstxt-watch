import gzip, json, shutil, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import crawler

FIXTURES = {'publishers.txt': 'pub-a.com\npub-b.com  # comment\nhttps://www.pub-c.com/\npub-d.com\n\n', 'run1/ads/pub-a.com.txt': '# comment line\ncontact=ads@pub-a.com\nbigssp.com, 111, DIRECT, abc123\nsmallssp.com, 222, RESELLER\nTiny-Net.com , 333 , reseller # inline comment\n', 'run1/ads/pub-b.com.txt': 'bigssp.com, 1, DIRECT\nsmallssp.com, 2, DIRECT\n', 'run1/ads/pub-c.com.txt': '\ufeffbigssp.com, 1, DIRECT\nsmallssp.com, 3, RESELLER\ngarbage line\n', 'run1/ads/www.pub-d.com.txt': 'bigssp.com, 9, DIRECT\nsmallssp.com, 9, DIRECT\n', 'run1/sellers/bigssp.com.json': '{"sellers":[{"seller_id":"1","name":"Pub A","domain":"pub-a.com","seller_type":"PUBLISHER"},\n{"seller_id":"50","name":"Tiny Network Inc.","domain":"tinynet.com","seller_type":"INTERMEDIARY"},\n{"seller_id":"51","name":"Mid Media LLC","domain":"midmedia.com","seller_type":"BOTH"},\n{"seller_id":"52","name":"Nameless Reseller","seller_type":"INTERMEDIARY"}]}\n', 'run1/sellers/smallssp.com.json': '{"sellers":[{"seller_id":"7","name":"Mid Media","domain":"midmedia.com","seller_type":"INTERMEDIARY"},\n{"seller_id":"8","name":"Tiny Network","domain":"www.tinynet.com","seller_type":"INTERMEDIARY"}]}\n', 'run2/ads/pub-a.com.txt': '# comment line\ncontact=ads@pub-a.com\nbigssp.com, 111, DIRECT, abc123\nsmallssp.com, 222, RESELLER\nTiny-Net.com , 333 , reseller # inline comment\n', 'run2/ads/pub-b.com.txt': 'bigssp.com, 1, DIRECT\n', 'run2/ads/pub-c.com.txt': 'bigssp.com, 1, DIRECT\n', 'run2/ads/pub-d.com.txt': '<!doctype html><html>not found</html>', 'run2/ads/www.pub-d.com.txt': '<!doctype html><html>not found</html>', 'run2/sellers/bigssp.com.json': '{"sellers":[{"seller_id":"1","name":"Pub A","domain":"pub-a.com","seller_type":"PUBLISHER"},\n{"seller_id":"51","name":"Mid Media LLC","domain":"midmedia.com","seller_type":"BOTH"},\n{"seller_id":"60","name":"NewCo Ads","domain":"newco.io","seller_type":"INTERMEDIARY"}]}\n', 'run2/sellers/smallssp.com.json': '{"sellers":[{"seller_id":"7","name":"Mid Media","domain":"midmedia.com","seller_type":"INTERMEDIARY"}]}\n'}

def make_fixtures():
    root = Path(tempfile.mkdtemp())
    for rel, text in FIXTURES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root

FIX = make_fixtures()


class Parsing(unittest.TestCase):
    def test_ads_txt(self):
        recs, var = crawler.parse_ads_txt(
            "﻿# c\ncontact=x@y.com\nA.com, 1, direct, cert\nb.com,2,RESELLER\nbad\nc.com, 3, OTHER\nd.com, , DIRECT\n")
        self.assertEqual(recs, [["a.com", "1", "DIRECT"], ["b.com", "2", "RESELLER"]])
        self.assertEqual(var, {"contact": ["x@y.com"]})

    def test_domain(self):
        self.assertEqual(crawler.normalise_domain("HTTPS://www.Foo.co.uk/x"), "foo.co.uk")
        self.assertEqual(crawler.normalise_domain("not a domain"), "")

    def test_company_key(self):
        self.assertEqual(crawler.company_key({"name": "Tiny Network Inc.", "domain": ""}), "name:tiny network")
        self.assertEqual(crawler.company_key({"name": "X", "domain": "x.com"}), "x.com")


class TwoRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_baseline_then_diff(self):
        pubs = FIX / "publishers.txt"
        md1 = crawler.run(crawler.FixtureFetcher(FIX / "run1"), pubs, self.tmp, "2026-09-23")
        self.assertIn("baseline", md1)
        self.assertIn("Publishers read OK: 4 of 4", md1)
        md2 = crawler.run(crawler.FixtureFetcher(FIX / "run2"), pubs, self.tmp, "2026-09-27")
        d = json.loads((self.tmp / "latest-diff.json").read_text())
        self.assertEqual(d["previous"], "2026-09-23")
        # pub-d failed in run 2, so it is excluded and never counted as a loss
        self.assertEqual(d["compared_publishers"], 3)
        small = [c for c in d["adsys"] if c["adsys"] == "smallssp.com"][0]
        self.assertEqual((small["before"], small["after"], small["lost"]), (3, 1, ["pub-b.com", "pub-c.com"]))
        self.assertNotIn("pub-d.com", json.dumps(d["adsys"]))
        comp = {c["key"]: c for c in d["companies"]}
        self.assertEqual(comp["tinynet.com"]["exchanges_after"], 0)      # gone everywhere
        self.assertEqual(comp["tinynet.com"]["exchanges_before"], 2)     # www. normalised, counted once per exchange
        self.assertEqual(comp["newco.io"]["exchanges_before"], 0)        # new
        self.assertNotIn("midmedia.com", comp)                          # unchanged, not reported
        self.assertIn("Tiny Network", md2)
        self.assertIn("NewCo Ads", md2)
        self.assertIn("Publishers read OK: 3 of 4", md2)
        # outputs exist
        for f in ["latest-diff.md", "adsystems.csv", "intermediaries.csv", "latest-run.json", "history/2026-09-27.md"]:
            self.assertTrue((self.tmp / f).exists(), f)
        run = json.loads((self.tmp / "latest-run.json").read_text())
        self.assertIn("pub-d.com", run["errors"])

    def test_same_day_rerun_compares_with_earlier_day(self):
        pubs = FIX / "publishers.txt"
        crawler.run(crawler.FixtureFetcher(FIX / "run1"), pubs, self.tmp, "2026-09-23")
        crawler.run(crawler.FixtureFetcher(FIX / "run2"), pubs, self.tmp, "2026-09-27")
        crawler.run(crawler.FixtureFetcher(FIX / "run2"), pubs, self.tmp, "2026-09-27")
        d = json.loads((self.tmp / "latest-diff.json").read_text())
        self.assertEqual(d["previous"], "2026-09-23")

    def test_size_cap(self):
        old = crawler.SELLERS_MAX
        crawler.SELLERS_MAX = 10
        try:
            r = crawler.get_sellers(crawler.FixtureFetcher(FIX / "run1"), "bigssp.com")
        finally:
            crawler.SELLERS_MAX = old
        self.assertEqual(r["status"], "too_large")


if __name__ == "__main__":
    unittest.main()
