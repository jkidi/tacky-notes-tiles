"""Unit tests for list_extracts.py. Uses a small, hand-built fixture standing
in for Geofabrik's real index-v1-nogeom.json -- never a real network call
(the retry tests talk to a local server only).
Run with:

    python -m unittest test_list_extracts.py
"""

import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import list_extracts as le  # noqa: E402


def _props(id_, parent=None, iso=None, path=None):
    """A minimal feature-properties dict, shaped like one entry of Geofabrik's
    index-v1-nogeom.json (see list_extracts.py's module docstring)."""
    p = {"id": id_, "urls": {"pbf": f"https://download.geofabrik.de/{path or id_}-latest.osm.pbf"}}
    if parent is not None:
        p["parent"] = parent
    if iso is not None:
        p["iso3166-1:alpha2"] = [iso]
    return p


# A small fixture mixing both Geofabrik nesting conventions:
# - "europe" has ordinary countries (poland: parent-based subdivisions) and a
#   multi-country convenience extract with no ISO code ("dach").
# - "north-america" has a country whose subdivisions are flattened directly
#   under the continent with an id-prefix convention ("us/california" etc,
#   parent "north-america", not "us").
FIXTURE_PROPS_BY_ID = {
    "europe": _props("europe"),
    "poland": _props("poland", parent="europe", iso="PL", path="europe/poland"),
    "mazowieckie": _props("mazowieckie", parent="poland", path="europe/poland/mazowieckie"),
    "malopolskie": _props("malopolskie", parent="poland", path="europe/poland/malopolskie"),
    "andorra": _props("andorra", parent="europe", iso="AD", path="europe/andorra"),
    "dach": _props("dach", parent="europe", path="europe/dach"),
    "north-america": _props("north-america"),
    "us": _props("us", parent="north-america", iso="US", path="north-america/us"),
    "us/california": _props("us/california", parent="north-america", path="north-america/us/california"),
    "us/nevada": _props("us/nevada", parent="north-america", path="north-america/us/nevada"),
    "us-midwest": _props("us-midwest", parent="north-america", path="north-america/us-midwest"),
    "canada": _props("canada", parent="north-america", iso="CA", path="north-america/canada"),
}

SIZES = {
    "poland": 2_101_321_402,
    "andorra": 3_487_221,
    "us": 12_167_171_310,
    "canada": 6_501_385_076,
}


def _size_from_fixture(props: dict) -> int:
    return SIZES[props["id"]]


class RegionPathTest(unittest.TestCase):
    def test_ordinary_nested_country(self):
        self.assertEqual(le.region_path(FIXTURE_PROPS_BY_ID["mazowieckie"]), "europe/poland/mazowieckie")

    def test_id_prefixed_subdivision(self):
        self.assertEqual(le.region_path(FIXTURE_PROPS_BY_ID["us/california"]), "north-america/us/california")

    def test_top_level_region(self):
        self.assertEqual(le.region_path(FIXTURE_PROPS_BY_ID["europe"]), "europe")


class CountryCandidatesTest(unittest.TestCase):
    def test_excludes_extracts_without_an_iso_code(self):
        candidates = le.country_candidates("europe", FIXTURE_PROPS_BY_ID)
        self.assertIn("poland", candidates)
        self.assertIn("andorra", candidates)
        self.assertNotIn("dach", candidates)

    def test_excludes_id_prefixed_subdivisions_even_with_an_iso_code(self):
        candidates = le.country_candidates("north-america", FIXTURE_PROPS_BY_ID)
        self.assertIn("us", candidates)
        self.assertIn("canada", candidates)
        self.assertNotIn("us/california", candidates)
        self.assertNotIn("us-midwest", candidates)


class ChildrenOfTest(unittest.TestCase):
    def test_parent_based_children(self):
        self.assertEqual(le.children_of("poland", FIXTURE_PROPS_BY_ID), ["malopolskie", "mazowieckie"])

    def test_id_prefix_based_children(self):
        self.assertEqual(le.children_of("us", FIXTURE_PROPS_BY_ID), ["us/california", "us/nevada"])

    def test_no_children_for_a_country_with_none_listed(self):
        self.assertEqual(le.children_of("andorra", FIXTURE_PROPS_BY_ID), [])


class SelectExtractsTest(unittest.TestCase):
    def test_small_country_is_kept_whole(self):
        paths = le.select_extracts("europe", {"europe": FIXTURE_PROPS_BY_ID["europe"], "andorra": FIXTURE_PROPS_BY_ID["andorra"]}, _size_from_fixture)
        self.assertEqual(paths, ["europe/andorra"])

    def test_big_country_is_replaced_by_its_children(self):
        subset = {k: FIXTURE_PROPS_BY_ID[k] for k in ["europe", "poland", "mazowieckie", "malopolskie"]}
        paths = le.select_extracts("europe", subset, _size_from_fixture)
        self.assertEqual(sorted(paths), ["europe/poland/malopolskie", "europe/poland/mazowieckie"])

    def test_convenience_extract_without_iso_is_never_selected(self):
        paths = le.select_extracts("europe", FIXTURE_PROPS_BY_ID, _size_from_fixture)
        self.assertNotIn("europe/dach", paths)

    def test_id_prefixed_big_country_is_replaced_by_its_children(self):
        subset = {k: FIXTURE_PROPS_BY_ID[k] for k in ["north-america", "us", "us/california", "us/nevada"]}
        paths = le.select_extracts("north-america", subset, _size_from_fixture)
        self.assertEqual(sorted(paths), ["north-america/us/california", "north-america/us/nevada"])

    def test_big_country_with_no_children_is_kept_whole_with_a_warning(self):
        subset = {"north-america": FIXTURE_PROPS_BY_ID["north-america"], "canada": FIXTURE_PROPS_BY_ID["canada"]}
        paths = le.select_extracts("north-america", subset, _size_from_fixture)
        self.assertEqual(paths, ["north-america/canada"])

    def test_threshold_is_configurable(self):
        # Andorra (3.5MB) is "big" relative to a 1KB threshold.
        subset = {"europe": FIXTURE_PROPS_BY_ID["europe"], "andorra": FIXTURE_PROPS_BY_ID["andorra"]}
        paths = le.select_extracts("europe", subset, _size_from_fixture, size_threshold_bytes=1_000)
        # No children listed for andorra -> kept whole despite being "big".
        self.assertEqual(paths, ["europe/andorra"])

    def test_full_fixture_end_to_end(self):
        europe_paths = le.select_extracts("europe", FIXTURE_PROPS_BY_ID, _size_from_fixture)
        self.assertEqual(
            sorted(europe_paths),
            ["europe/andorra", "europe/poland/malopolskie", "europe/poland/mazowieckie"],
        )

        na_paths = le.select_extracts("north-america", FIXTURE_PROPS_BY_ID, _size_from_fixture)
        self.assertEqual(
            sorted(na_paths),
            ["north-america/canada", "north-america/us/california", "north-america/us/nevada"],
        )

    def test_extra_extracts_is_omitted_by_default(self):
        # Callers who don't pass extra_extracts (e.g. every other test above)
        # get exactly the ISO-code-based selection, unaffected by the real
        # EXTRA_EXTRACTS constant.
        paths = le.select_extracts("europe", FIXTURE_PROPS_BY_ID, _size_from_fixture)
        self.assertNotIn("europe/kosovo", paths)

    def test_extra_extracts_are_appended_for_their_region(self):
        paths = le.select_extracts(
            "europe",
            {"europe": FIXTURE_PROPS_BY_ID["europe"], "andorra": FIXTURE_PROPS_BY_ID["andorra"]},
            _size_from_fixture,
            extra_extracts={"europe": ["europe/kosovo", "europe/azores"]},
        )
        self.assertEqual(paths, ["europe/andorra", "europe/kosovo", "europe/azores"])

    def test_extra_extracts_for_a_different_region_are_not_appended(self):
        paths = le.select_extracts(
            "north-america",
            {"north-america": FIXTURE_PROPS_BY_ID["north-america"], "canada": FIXTURE_PROPS_BY_ID["canada"]},
            _size_from_fixture,
            extra_extracts={"europe": ["europe/kosovo"]},
        )
        self.assertEqual(paths, ["north-america/canada"])

    def test_the_real_extra_extracts_constant_covers_the_known_iso_code_gap_territories(self):
        paths = le.select_extracts(
            "europe",
            {"europe": FIXTURE_PROPS_BY_ID["europe"], "andorra": FIXTURE_PROPS_BY_ID["andorra"]},
            _size_from_fixture,
            extra_extracts=le.EXTRA_EXTRACTS,
        )
        for expected in [
            "europe/kosovo",
            "europe/azores",
            "europe/isle-of-man",
            "europe/guernsey-jersey",
            "africa/canary-islands",
        ]:
            self.assertIn(expected, paths)


class IndexByIdTest(unittest.TestCase):
    def test_parses_a_geojson_style_index(self):
        index_json = {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "properties": _props("andorra", parent="europe", iso="AD", path="europe/andorra")}
            ],
        }
        result = le.index_by_id(index_json)
        self.assertEqual(list(result.keys()), ["andorra"])
        self.assertEqual(result["andorra"]["id"], "andorra")


class RetryTest(unittest.TestCase):
    """Geofabrik stalling or failing for a moment doesn't fail the build."""

    def serve(self, failures, stalls=0):
        """A local server answering HEAD and GET with 503 [failures] times, then
        200; the first [stalls] requests first hang for a second."""
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def answer(self, body):
                calls.append(self.command)
                if len(calls) <= stalls:
                    time.sleep(1)
                if len(calls) <= failures:
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", "1234" if not body else str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_HEAD(self):
                self.answer(None)

            def do_GET(self):
                self.answer(b'{"features": []}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        # No waiting between retries in a test.
        session = le.http_session(le.RETRY.new(backoff_factor=0))
        patcher = mock.patch.object(le, "SESSION", session)
        patcher.start()
        self.addCleanup(patcher.stop)
        return f"http://127.0.0.1:{server.server_address[1]}/x", calls

    def test_head_retries_until_geofabrik_answers(self):
        url, calls = self.serve(failures=2)
        self.assertEqual(le.http_head_content_length({"urls": {"pbf": url}}), 1234)
        self.assertEqual(calls, ["HEAD", "HEAD", "HEAD"])

    def test_index_download_retries_until_geofabrik_answers(self):
        url, calls = self.serve(failures=2)
        self.assertEqual(le.fetch_index(url), {"features": []})
        self.assertEqual(len(calls), 3)

    def test_gives_up_after_five_retries(self):
        url, calls = self.serve(failures=100)
        with self.assertRaises(le.requests.HTTPError):
            le.http_head_content_length({"urls": {"pbf": url}})
        self.assertEqual(len(calls), 6)

    def test_retries_a_request_that_stalls_past_the_timeout(self):
        url, calls = self.serve(failures=0, stalls=1)
        with mock.patch.object(le, "REQUEST_TIMEOUT_SECONDS", 0.3):
            self.assertEqual(le.http_head_content_length({"urls": {"pbf": url}}), 1234)
        self.assertEqual(calls, ["HEAD", "HEAD"])


if __name__ == "__main__":
    unittest.main()
