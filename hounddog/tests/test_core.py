from __future__ import annotations

import sqlite3
import tempfile
import unittest

from hounddog.collectors import parse_nws, parse_rss, parse_usgs
from hounddog.db import claim_detail, connect, ingest, transition
from hounddog.formatters import ghostnet_message
from hounddog.model import Observation, SourceConfig


class HoundDogTests(unittest.TestCase):
    def source(self, kind="rss", source_type="community", family="forum-a"):
        return SourceConfig("test", kind, "Test Source", "https://example.test/feed", source_type, family)

    def test_nws_parser_preserves_source_and_location(self):
        payload = b'{"features":[{"id":"urn:1","properties":{"event":"Tornado Warning","headline":"Tornado Warning issued","areaDesc":"Knox County, TN","severity":"Extreme","sent":"2026-09-21T01:00:00Z","description":"Take cover"}}]}'
        item = parse_nws(self.source("nws_alerts", "official", "nws"), payload)[0]
        self.assertEqual(item.location, "Knox County, TN")
        self.assertEqual(item.severity, "extreme")
        self.assertEqual(item.source_family, "nws")

    def test_usgs_parser(self):
        payload = b'{"features":[{"id":"us1","properties":{"mag":6.2,"title":"M 6.2 - Test Region","place":"Test Region","time":1789952400000,"url":"https://example.test/us1","tsunami":0}}]}'
        item = parse_usgs(self.source("usgs_geojson", "official", "usgs"), payload)[0]
        self.assertEqual(item.category, "earthquake")
        self.assertEqual(item.severity, "severe")

    def test_rss_parser(self):
        feed = b'<rss><channel><item><guid>x1</guid><title>Possible incident</title><description>Unconfirmed report</description><link>https://example.test/x1</link><pubDate>Sun, 21 Sep 2026 01:00:00 GMT</pubDate></item></channel></rss>'
        item = parse_rss(self.source(), feed)[0]
        self.assertEqual(item.external_id, "x1")
        self.assertEqual(item.source_type, "community")

    def test_repeats_do_not_become_independent_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/test.db"
            with connect(path) as db:
                first = Observation("a", "A", "community", "telegram-origin", "1", "Possible fuel outage in Knox", "", "https://a")
                second = Observation("b", "B mirror", "community", "telegram-origin", "2", "Possible fuel outage in Knox", "", "https://b")
                claim_id, _ = ingest(db, first)
                ingest(db, second)
                claim, _ = claim_detail(db, claim_id)
                self.assertEqual(claim["independent_families"], 1)
                self.assertEqual(claim["confidence_label"], "UNVERIFIED")

    def test_independent_official_source_confirms_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/test.db"
            with connect(path) as db:
                first = Observation("a", "Monitor", "community", "monitor-a", "1", "Major fire near Knoxville terminal", "", "https://a", category="fire", location="Knoxville TN", severity="severe")
                official = Observation("b", "Fire Agency", "official", "agency-b", "2", "Major fire near Knoxville terminal", "", "https://b", category="fire", location="Knoxville TN", severity="severe")
                claim_id, _ = ingest(db, first)
                ingest(db, official)
                claim, rows = claim_detail(db, claim_id)
                self.assertEqual(claim["confidence_label"], "CONFIRMED")
                message = ghostnet_message(claim, rows, callsign="KQ4ODY", network="GHOSTNET", max_chars=240)
                self.assertIn("@GHOSTNET CONFIRMED/", message)
                self.assertLessEqual(len(message), 240)

    def test_workflow_prevents_skipping_human_review(self):
        with tempfile.TemporaryDirectory() as folder:
            path = f"{folder}/test.db"
            with connect(path) as db:
                claim_id, _ = ingest(db, Observation("a", "A", "official", "a", "1", "Test alert", "", "https://a"))
                with self.assertRaises(ValueError):
                    transition(db, claim_id, "SENT")
                transition(db, claim_id, "REVIEW")
                transition(db, claim_id, "TX_CANDIDATE")
                transition(db, claim_id, "SENT")


if __name__ == "__main__":
    unittest.main()

