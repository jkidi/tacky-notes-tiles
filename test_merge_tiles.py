"""Unit tests for merge_tiles.py: cross-directory dedupe-by-osmId and the
total-size check. Run with:

    python -m unittest test_merge_tiles.py
"""

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import merge_tiles as mt  # noqa: E402


def _write_tile(directory: Path, file_name: str, pois: list[dict]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / file_name).write_bytes(gzip.compress(json.dumps(pois).encode("utf-8")))


class TileKeyFromFileNameTest(unittest.TestCase):
    def test_positive_indices(self):
        self.assertEqual(mt.tile_key_from_file_name("250_99.json.gz"), (250, 99))

    def test_negative_indices(self):
        self.assertEqual(mt.tile_key_from_file_name("-1_-1.json.gz"), (-1, -1))

    def test_rejects_an_unexpected_file_name(self):
        with self.assertRaises(ValueError):
            mt.tile_key_from_file_name("not-a-tile.json.gz")


class MergeDirectoriesTest(unittest.TestCase):
    def test_merges_disjoint_tiles_from_two_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            region_a = root / "a"
            region_b = root / "b"
            poi_a = {"osmId": "node/1", "lat": 50.05, "lng": 19.95, "category": "shop=supermarket"}
            poi_b = {"osmId": "node/2", "lat": 10.0, "lng": 10.0, "category": "amenity=cafe"}
            _write_tile(region_a, "250_99.json.gz", [poi_a])
            _write_tile(region_b, "50_50.json.gz", [poi_b])

            tiles = mt.merge_directories([region_a, region_b])

            self.assertEqual(set(tiles.keys()), {(250, 99), (50, 50)})
            self.assertEqual(tiles[(250, 99)], {"node/1": poi_a})
            self.assertEqual(tiles[(50, 50)], {"node/2": poi_b})

    def test_a_border_poi_present_in_two_regions_is_deduped(self):
        """The Review Focus scenario: a POI near a regional border appears in
        both region extracts (and therefore both region tile directories),
        but the merged output has it exactly once."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            region_a = root / "europe-poland"
            region_b = root / "europe-germany"
            border_poi = {"osmId": "node/42", "lat": 52.0, "lng": 15.0, "category": "amenity=fuel"}
            _write_tile(region_a, "260_75.json.gz", [border_poi])
            _write_tile(region_b, "260_75.json.gz", [border_poi])

            tiles = mt.merge_directories([region_a, region_b])

            self.assertEqual(len(tiles), 1)
            self.assertEqual(tiles[(260, 75)], {"node/42": border_poi})

    def test_later_directory_wins_on_conflicting_content_for_the_same_osm_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            region_a = root / "a"
            region_b = root / "b"
            _write_tile(region_a, "1_1.json.gz", [{"osmId": "node/1", "lat": 1.0, "lng": 1.0, "name": "Old", "category": "amenity=cafe"}])
            _write_tile(region_b, "1_1.json.gz", [{"osmId": "node/1", "lat": 1.0, "lng": 1.0, "name": "New", "category": "amenity=cafe"}])

            tiles = mt.merge_directories([region_a, region_b])

            self.assertEqual(tiles[(1, 1)]["node/1"]["name"], "New")

    def test_empty_directories_merge_to_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "b").mkdir()

            tiles = mt.merge_directories([root / "a", root / "b"])

            self.assertEqual(tiles, {})


class MainSizeCheckTest(unittest.TestCase):
    def test_exits_zero_and_writes_output_under_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            region_a = root / "a"
            _write_tile(region_a, "1_1.json.gz", [{"osmId": "node/1", "lat": 1.0, "lng": 1.0, "category": "amenity=cafe"}])
            out_dir = root / "merged"

            exit_code = mt.main([str(region_a), "--out", str(out_dir), "--max-bytes", "1000000"])

            self.assertEqual(exit_code, 0)
            self.assertTrue((out_dir / "1_1.json.gz").exists())

    def test_exits_nonzero_but_still_writes_output_over_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            region_a = root / "a"
            _write_tile(region_a, "1_1.json.gz", [{"osmId": "node/1", "lat": 1.0, "lng": 1.0, "category": "amenity=cafe"}])
            out_dir = root / "merged"

            # A max-bytes of 0 is always exceeded by any non-empty tile set.
            exit_code = mt.main([str(region_a), "--out", str(out_dir), "--max-bytes", "0"])

            self.assertEqual(exit_code, 1)
            self.assertTrue((out_dir / "1_1.json.gz").exists())

    def test_default_max_bytes_is_900_million(self):
        self.assertEqual(mt.DEFAULT_MAX_BYTES, 900_000_000)


if __name__ == "__main__":
    unittest.main()
