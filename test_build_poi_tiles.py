"""Pure unit tests for build_poi_tiles.py's tag matching and tile binning --
no .osm.pbf parsing (that's exercised by hand against a small Geofabrik
extract; see README.md). Run with:

    python -m unittest test_build_poi_tiles.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_poi_tiles as bpt  # noqa: E402


class CategoryForTagsTest(unittest.TestCase):
    def test_matches_a_curated_tag(self):
        self.assertEqual(bpt.category_for_tags({"shop": "supermarket"}), "shop=supermarket")

    def test_matches_amongst_other_unrelated_tags(self):
        tags = {"name": "Foo", "shop": "bakery", "opening_hours": "24/7"}
        self.assertEqual(bpt.category_for_tags(tags), "shop=bakery")

    def test_no_curated_tag_returns_none(self):
        self.assertIsNone(bpt.category_for_tags({"shop": "boutique", "name": "Foo"}))

    def test_empty_tags_returns_none(self):
        self.assertIsNone(bpt.category_for_tags({}))

    def test_same_key_different_value_does_not_match(self):
        # amenity=fuel is curated, amenity=theatre is not.
        self.assertIsNone(bpt.category_for_tags({"amenity": "theatre"}))

    def test_parking_is_not_curated(self):
        # Dropped in phase 6a -- see build_poi_tiles.py's CURATED_TAGS docstring.
        self.assertIsNone(bpt.category_for_tags({"amenity": "parking"}))

    def test_all_21_curated_tags_are_recognised(self):
        for tag in bpt.CURATED_TAGS:
            key, value = tag.split("=", 1)
            self.assertEqual(bpt.category_for_tags({key: value}), tag)
        self.assertEqual(len(bpt.CURATED_TAGS), 21)


class BrandWikidataIdForTagsTest(unittest.TestCase):
    """No brand catalog any more (this pipeline no longer needs the app's
    brand catalog) -- any tag value that looks like a Wikidata entity id is
    kept."""

    def test_wikidata_id_is_returned(self):
        tags = {"brand:wikidata": "Q38076"}
        self.assertEqual(bpt.brand_wikidata_id_for_tags(tags), "Q38076")

    def test_long_wikidata_id_is_returned(self):
        tags = {"brand:wikidata": "Q123456789"}
        self.assertEqual(bpt.brand_wikidata_id_for_tags(tags), "Q123456789")

    def test_missing_tag_returns_none(self):
        self.assertIsNone(bpt.brand_wikidata_id_for_tags({}))

    def test_empty_value_returns_none(self):
        self.assertIsNone(bpt.brand_wikidata_id_for_tags({"brand:wikidata": ""}))

    def test_malformed_value_is_dropped(self):
        for bad in ["Q", "38076", "Q38076x", "P123", "Q-1", " Q38076"]:
            with self.subTest(bad=bad):
                self.assertIsNone(bpt.brand_wikidata_id_for_tags({"brand:wikidata": bad}))


class PoiFromTagsTest(unittest.TestCase):
    def test_no_category_match_returns_none_even_with_a_wikidata_brand(self):
        tags = {"shop": "boutique", "brand:wikidata": "Q38076", "name": "Foo"}
        self.assertIsNone(bpt.poi_from_tags("node/1", 1.0, 2.0, tags))

    def test_category_match_without_brand(self):
        tags = {"amenity": "cafe", "name": "Joe's"}
        poi = bpt.poi_from_tags("node/1", 1.5, 2.5, tags)
        self.assertEqual(
            poi,
            {"osmId": "node/1", "lat": 1.5, "lng": 2.5, "name": "Joe's", "category": "amenity=cafe"},
        )
        self.assertNotIn("brandWikidataId", poi)

    def test_category_match_with_wikidata_brand(self):
        tags = {"amenity": "fast_food", "brand:wikidata": "Q38076", "name": "McDonald's"}
        poi = bpt.poi_from_tags("node/2", 1.0, 1.0, tags)
        self.assertEqual(poi["brandWikidataId"], "Q38076")
        self.assertEqual(poi["category"], "amenity=fast_food")

    def test_category_match_with_malformed_brand_omits_brand_field(self):
        tags = {"amenity": "fast_food", "brand:wikidata": "not-a-qid", "name": "Copycat"}
        poi = bpt.poi_from_tags("node/3", 1.0, 1.0, tags)
        self.assertNotIn("brandWikidataId", poi)

    def test_missing_name_omits_name_field(self):
        tags = {"shop": "supermarket"}
        poi = bpt.poi_from_tags("node/4", 1.0, 1.0, tags)
        self.assertNotIn("name", poi)

    def test_empty_name_omits_name_field(self):
        tags = {"shop": "supermarket", "name": ""}
        poi = bpt.poi_from_tags("node/5", 1.0, 1.0, tags)
        self.assertNotIn("name", poi)

    def test_parking_tag_is_never_a_poi(self):
        tags = {"amenity": "parking", "name": "Some car park"}
        self.assertIsNone(bpt.poi_from_tags("way/1", 1.0, 1.0, tags))


class TileIndexTest(unittest.TestCase):
    def test_matches_dart_floor_semantics_for_positive_coordinates(self):
        self.assertEqual(bpt.tile_index(50.05, 19.95), (250, 99))

    def test_matches_dart_floor_semantics_at_a_cell_boundary(self):
        # 50.0 / 0.2 == 250.0 exactly -> floor is still 250, not 249.
        self.assertEqual(bpt.tile_index(50.0, 20.0), (250, 100))

    def test_negative_coordinates_floor_towards_negative_infinity(self):
        # -0.1 / 0.2 == -0.5 -> floor(-0.5) == -1, matching Dart's
        # double.floor(), not Python's int() truncation (which would give 0).
        self.assertEqual(bpt.tile_index(-0.1, -0.1), (-1, -1))

    def test_negative_coordinate_at_exact_boundary(self):
        self.assertEqual(bpt.tile_index(-0.2, -0.2), (-1, -1))


class TileFileNameTest(unittest.TestCase):
    def test_positive_indices(self):
        self.assertEqual(bpt.tile_file_name(250, 99), "250_99.json.gz")

    def test_negative_indices(self):
        self.assertEqual(bpt.tile_file_name(-1, -1), "-1_-1.json.gz")


class PolygonAreaAndCentroidTest(unittest.TestCase):
    def test_unit_square_centroid_is_its_center(self):
        points = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        area, cx, cy = bpt._polygon_area_and_centroid(points)
        self.assertAlmostEqual(abs(area), 1.0)
        self.assertAlmostEqual(cx, 0.5)
        self.assertAlmostEqual(cy, 0.5)

    def test_degenerate_two_point_ring_falls_back_to_average(self):
        points = [(0.0, 0.0), (2.0, 4.0)]
        area, cx, cy = bpt._polygon_area_and_centroid(points)
        self.assertEqual(area, 0.0)
        self.assertAlmostEqual(cx, 1.0)
        self.assertAlmostEqual(cy, 2.0)

    def test_collinear_points_fall_back_to_average(self):
        points = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
        area, cx, cy = bpt._polygon_area_and_centroid(points)
        self.assertEqual(area, 0.0)
        self.assertAlmostEqual(cx, 1.0)
        self.assertAlmostEqual(cy, 0.0)

    def test_empty_ring(self):
        area, cx, cy = bpt._polygon_area_and_centroid([])
        self.assertEqual((area, cx, cy), (0.0, 0.0, 0.0))


class PoiHandlerBinningTest(unittest.TestCase):
    """Exercises PoiHandler's tile-binning/dedup/counting logic directly via
    _handle, without going through real osmium node/area callbacks."""

    def setUp(self):
        self.handler = bpt.PoiHandler()

    def test_bins_into_the_containing_tile(self):
        self.handler._handle("node/1", 50.05, 19.95, {"shop": "supermarket"})
        self.assertIn((250, 99), self.handler.tiles)
        self.assertEqual(self.handler.tiles[(250, 99)]["node/1"]["category"], "shop=supermarket")

    def test_non_matching_tags_are_not_binned(self):
        self.handler._handle("node/1", 50.05, 19.95, {"shop": "boutique"})
        self.assertEqual(self.handler.tiles, {})

    def test_parking_is_not_binned(self):
        self.handler._handle("way/1", 50.05, 19.95, {"amenity": "parking"})
        self.assertEqual(self.handler.tiles, {})

    def test_dedupes_by_osm_id_within_a_tile(self):
        self.handler._handle("node/1", 50.05, 19.95, {"shop": "supermarket", "name": "First"})
        self.handler._handle("node/1", 50.06, 19.96, {"shop": "supermarket", "name": "Second"})
        tile = self.handler.tiles[(250, 99)]
        self.assertEqual(len(tile), 1)
        self.assertEqual(tile["node/1"]["name"], "Second")

    def test_total_poi_count_sums_across_tiles(self):
        self.handler._handle("node/1", 50.05, 19.95, {"shop": "supermarket"})
        self.handler._handle("node/2", 10.0, 10.0, {"amenity": "cafe"})
        self.assertEqual(self.handler.total_poi_count(), 2)

    def test_category_and_brand_counts(self):
        self.handler._handle("node/1", 1.0, 1.0, {"amenity": "fast_food", "brand:wikidata": "Q38076"})
        self.handler._handle("node/2", 1.0, 1.0, {"amenity": "fast_food"})
        self.assertEqual(self.handler.category_counts["amenity=fast_food"], 2)
        self.assertEqual(self.handler.brand_counts["Q38076"], 1)


class IndexArgForExtractTest(unittest.TestCase):
    def test_in_memory_type_passed_through_unchanged(self):
        result = bpt.index_arg_for_extract("flex_mem", Path("poland.osm.pbf"), Path("/tmp/idx"))
        self.assertEqual(result, "flex_mem")

    def test_file_backed_type_gets_a_path_unique_to_the_extract(self):
        result = bpt.index_arg_for_extract(
            "sparse_file_array,/ignored/template", Path("/data/mazowieckie-latest.osm.pbf"), Path("/tmp/idx")
        )
        self.assertEqual(result, "sparse_file_array," + str(Path("/tmp/idx/mazowieckie-latest.osm.nodecache")))

    def test_different_extracts_get_different_paths(self):
        tmp_dir = Path("/tmp/idx")
        a = bpt.index_arg_for_extract("dense_file_array,/x", Path("poland.osm.pbf"), tmp_dir)
        b = bpt.index_arg_for_extract("dense_file_array,/x", Path("andorra.osm.pbf"), tmp_dir)
        self.assertNotEqual(a, b)


class WriteTilesTest(unittest.TestCase):
    def test_writes_gzip_json_and_clears_stale_tiles(self):
        import gzip
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            stale = out_dir / "9_9.json.gz"
            stale.write_bytes(b"stale")

            tiles = {
                (250, 99): {"node/1": {"osmId": "node/1", "lat": 50.05, "lng": 19.95, "category": "shop=supermarket"}}
            }
            tile_count, total_bytes = bpt.write_tiles(tiles, out_dir)

            self.assertEqual(tile_count, 1)
            self.assertGreater(total_bytes, 0)
            self.assertFalse(stale.exists())

            written = out_dir / "250_99.json.gz"
            self.assertTrue(written.exists())
            decoded = json.loads(gzip.decompress(written.read_bytes()))
            self.assertEqual(decoded, [{"osmId": "node/1", "lat": 50.05, "lng": 19.95, "category": "shop=supermarket"}])


if __name__ == "__main__":
    unittest.main()
