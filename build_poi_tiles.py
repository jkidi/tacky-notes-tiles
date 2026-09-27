#!/usr/bin/env python3
"""Builds the pre-built POI tile grid from OSM extracts (locationnotes spec
§5.6/§7 -- see the locationnotes app repo's
docs/superpowers/specs/2026-09-26-locationnotes-v1-design.md).

Usage:
    python build_poi_tiles.py <extract.osm.pbf> [<extract2.osm.pbf> ...] --out <dir> \
        [--index <type>]

Scans every node and every way/multipolygon-relation area in the given
extract(s) for a tag matching one of the curated categories (CURATED_TAGS
below -- duplicated from the app repo's lib/poi/poi_category.dart's
PoiCategory.ALL; keep the two lists in sync) and bins the matches into the 0.2
degree x 0.2 degree tile grid used by the app's lib/poi/poi_tiles.dart
(PoiTile.containing: floor(lat/0.2), floor(lng/0.2)). Each non-empty tile is
written as `<out>/<latIndex>_<lngIndex>.json.gz`: a gzip-compressed JSON array
of `{"osmId", "lat", "lng", "name"?, "brandWikidataId"?, "category"}` objects,
matching the shape lib/poi/tile_poi_source.dart's TilePoiSource._decodeTile
reads. Tile URL format and JSON shape are unchanged from the pipeline's
previous home in the app repo (tool/poi_tiles/) -- only the build process
moved.

Only a POI whose tags match a curated category is included -- a brand tag
alone (without a matching category tag) is not enough, since the tile
schema's `category` field is required, not optional. This pipeline does not
carry or consult the app's brand catalog (assets/poi/brands.json): when a
curated match is found, `brandWikidataId` is set directly from the OSM
`brand:wikidata` tag whenever that value looks like a Wikidata entity id
(`^Q\\d+$`) -- an unrecognized-looking value (typo, wrong namespace, empty
string) is dropped rather than passed through. Validating that the id refers
to an actual, known brand is left to the app, which already treats any tile
`brandWikidataId` as untrusted-until-matched input.

A way/relation's location is its largest outer ring's area-weighted centroid
(see `_polygon_area_and_centroid`); this is an approximation (multipolygons
with several same-size outer rings pick one arbitrarily, and the centroid is
computed in plain lat/lng rather than a projected plane) that's adequate for
placing a POI within the search radii this app uses (hundreds of metres to
30 km) -- exact for any point actually near a straight-edged polygon.

Resolving way/area node coordinates needs a location index (`--index`,
default `flex_mem`); see `index_arg_for_extract` and README.md "Memory and
the pre-filter step" for the in-memory-vs-file-backed tradeoff on large
extracts. In CI, extracts are pre-filtered with `osmium tags-filter` before
reaching this script (see README.md and .github/workflows/build-tiles.yml),
which keeps memory use far below the app repo's original whole-country
measurements.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

import osmium

# Curated categories, as OSM `key=value` tags. Duplicated from the app repo's
# lib/poi/poi_category.dart's PoiCategory.ALL -- keep the two lists in sync by
# hand; there are exactly 21 (the phase-5 list minus `amenity=parking`, which
# generated far too much noise relative to its usefulness as an alert
# category -- see the app repo's phase 6a spec/plan for the decision).
CURATED_TAGS = frozenset(
    [
        "shop=supermarket",
        "shop=convenience",
        "amenity=pharmacy",
        "shop=chemist",
        "amenity=fuel",
        "amenity=charging_station",
        "amenity=cafe",
        "amenity=fast_food",
        "amenity=restaurant",
        "shop=bakery",
        "shop=doityourself",
        "amenity=post_office",
        "amenity=bank",
        "amenity=atm",
        "leisure=fitness_centre",
        "shop=books",
        "shop=electronics",
        "shop=clothes",
        "shop=pet",
        "shop=florist",
        "amenity=library",
    ]
)

TILE_SIZE_DEGREES = 0.2

# A Wikidata entity id, e.g. "Q259340". `brand:wikidata` values that don't
# match this (typos, a QID with extra suffix, a wrong-namespace id like a
# property "P..." id, an empty string) are dropped rather than passed
# through -- see poi_from_tags.
_WIKIDATA_ID_RE = re.compile(r"^Q\d+$")


def tile_index(lat: float, lng: float) -> tuple[int, int]:
    """(latIndex, lngIndex) -- matches PoiTile.containing in poi_tiles.dart
    exactly: floor(lat/0.2), floor(lng/0.2). Python's math.floor, like Dart's
    double.floor(), rounds towards negative infinity, so this agrees with the
    Dart side on negative coordinates too (e.g. lat=-0.1 -> index -1)."""
    return math.floor(lat / TILE_SIZE_DEGREES), math.floor(lng / TILE_SIZE_DEGREES)


def tile_file_name(lat_index: int, lng_index: int) -> str:
    """`<latIndex>_<lngIndex>.json.gz` -- matches PoiTile.fileName exactly."""
    return f"{lat_index}_{lng_index}.json.gz"


def category_for_tags(tags: dict[str, str]) -> str | None:
    """The curated `key=value` tag this element matches, or None. When an
    element happens to carry more than one curated tag (rare), the first one
    encountered in tag order wins -- there is only ever one `category` slot to
    fill."""
    for key, value in tags.items():
        kv = f"{key}={value}"
        if kv in CURATED_TAGS:
            return kv
    return None


def brand_wikidata_id_for_tags(tags: dict[str, str]) -> str | None:
    """The element's `brand:wikidata` tag value, but only when it looks like a
    Wikidata entity id (`^Q\\d+$`) -- anything else is dropped rather than
    passed through verbatim. There is no brand catalog lookup here (see the
    module docstring): this pipeline no longer knows or cares which brands
    the app curates."""
    brand_id = tags.get("brand:wikidata")
    if brand_id and _WIKIDATA_ID_RE.match(brand_id):
        return brand_id
    return None


def poi_from_tags(osm_id: str, lat: float, lng: float, tags: dict[str, str]) -> dict | None:
    """The tile-JSON dict for this element, or None when it doesn't match any
    curated category (in which case it's not a POI this tile grid tracks at
    all, brand or no brand)."""
    category = category_for_tags(tags)
    if category is None:
        return None

    poi = {"osmId": osm_id, "lat": lat, "lng": lng}

    name = tags.get("name")
    if name:
        poi["name"] = name

    brand_wikidata_id = brand_wikidata_id_for_tags(tags)
    if brand_wikidata_id:
        poi["brandWikidataId"] = brand_wikidata_id

    poi["category"] = category
    return poi


def _polygon_area_and_centroid(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """(signedArea, centroidLng, centroidLat) for a polygon ring given as
    (lng, lat) points (need not be explicitly closed -- the last point wraps
    around to the first). Uses the standard shoelace/area-weighted-centroid
    formula directly on lat/lng degrees, which is an adequate flat-plane
    approximation at the scale of a single OSM way. Degenerate rings (signed
    area ~0, e.g. fewer than 3 usable points, or collinear points) get area 0
    and a plain vertex average as their "centroid" instead of dividing by
    zero."""
    n = len(points)
    if n < 3:
        if n == 0:
            return 0.0, 0.0, 0.0
        avg_lng = sum(p[0] for p in points) / n
        avg_lat = sum(p[1] for p in points) / n
        return 0.0, avg_lng, avg_lat

    area = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    area *= 0.5

    if abs(area) < 1e-12:
        avg_lng = sum(p[0] for p in points) / n
        avg_lat = sum(p[1] for p in points) / n
        return 0.0, avg_lng, avg_lat

    cx /= 6 * area
    cy /= 6 * area
    return area, cx, cy


def area_centroid(area) -> tuple[float, float] | None:
    """(lat, lng) centroid of an osmium Area's largest (by area) outer ring,
    or None when every ring is unusable (e.g. no ring has a valid location for
    at least 3 of its nodes -- a way that crosses the extract's edge with
    missing neighbours)."""
    best_abs_area = -1.0
    best: tuple[float, float] | None = None

    for ring in area.outer_rings():
        points = []
        for node_ref in ring:
            try:
                points.append((node_ref.lon, node_ref.lat))
            except osmium.InvalidLocationError:
                continue

        signed_area, cx, cy = _polygon_area_and_centroid(points)
        if abs(signed_area) > best_abs_area:
            best_abs_area = abs(signed_area)
            best = (cy, cx)  # (lat, lng)

    return best


class PoiHandler(osmium.SimpleHandler):
    """Collects matching nodes and areas into tiles (keyed by (latIndex,
    lngIndex)), deduplicated by osmId within a tile -- so running this handler
    over more than one extract whose coverage overlaps at the edges doesn't
    double up a POI that appears in both files."""

    def __init__(self):
        super().__init__()
        self.tiles: dict[tuple[int, int], dict[str, dict]] = {}
        self.category_counts: Counter[str] = Counter()
        self.brand_counts: Counter[str] = Counter()

    def node(self, n) -> None:
        if not n.location.valid():
            return
        tags = {tag.k: tag.v for tag in n.tags}
        self._handle(f"node/{n.id}", n.location.lat, n.location.lon, tags)

    def area(self, a) -> None:
        centroid = area_centroid(a)
        if centroid is None:
            return
        lat, lng = centroid
        tags = {tag.k: tag.v for tag in a.tags}
        osm_type = "way" if a.from_way() else "relation"
        self._handle(f"{osm_type}/{a.orig_id()}", lat, lng, tags)

    def _handle(self, osm_id: str, lat: float, lng: float, tags: dict[str, str]) -> None:
        poi = poi_from_tags(osm_id, lat, lng, tags)
        if poi is None:
            return

        lat_index, lng_index = tile_index(lat, lng)
        tile = self.tiles.setdefault((lat_index, lng_index), {})
        tile[osm_id] = poi

        self.category_counts[poi["category"]] += 1
        brand_id = poi.get("brandWikidataId")
        if brand_id:
            self.brand_counts[brand_id] += 1

    def total_poi_count(self) -> int:
        return sum(len(tile) for tile in self.tiles.values())


def write_tiles(tiles: dict[tuple[int, int], dict[str, dict]], out_dir: Path) -> tuple[int, int]:
    """Writes one gzip-compressed JSON-array file per tile. Returns (tile
    count, total bytes written). Clears any `*.json.gz` files already in
    [out_dir] first, so a tile that no longer has any POIs (e.g. everything in
    it closed down) doesn't linger from a previous run -- this script always
    produces a full, current snapshot, never an incremental patch."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for existing in out_dir.glob("*.json.gz"):
        existing.unlink()

    total_bytes = 0
    for (lat_index, lng_index), pois_by_id in tiles.items():
        pois = [pois_by_id[osm_id] for osm_id in sorted(pois_by_id)]
        body = gzip.compress(json.dumps(pois, ensure_ascii=False).encode("utf-8"))

        file_path = out_dir / tile_file_name(lat_index, lng_index)
        tmp_path = out_dir / (tile_file_name(lat_index, lng_index) + ".tmp")
        tmp_path.write_bytes(body)
        tmp_path.replace(file_path)

        total_bytes += len(body)

    return len(tiles), total_bytes


def index_arg_for_extract(index_type: str, extract_path: Path, tmp_dir: Path) -> str:
    """The `idx` value passed to `SimpleHandler.apply_file` for [extract_path].

    [index_type] is one of osmium's location-index types (see
    `python -c "import osmium.index; print(osmium.index.map_types())"`),
    e.g. `flex_mem` (the default: in-memory, generally the fastest, but its
    memory use grows with the extract's node-id range) or a file-backed type
    such as `sparse_file_array` (bounded memory, backed by disk, slower).

    A file-backed type is written as `<kind>,<path>` (osmium's own syntax).
    The `<path>` half of [index_type] is only a placeholder here -- it's
    discarded and replaced outright, not reused as a base/prefix, with a path
    unique to [extract_path] inside [tmp_dir] (`main` creates one fresh
    per-run temp directory for this). That's deliberate: when this script
    processes several extracts in one run (see `main`), reusing one literal
    path across them would let a later extract's index reuse (and get
    confused by) an earlier extract's now-stale backing file for an unrelated
    node-id range. Only the `<kind>` half before the comma is taken from
    [index_type] as given. An in-memory type (no comma) is returned
    unchanged; there's no file at all to place, so nothing to substitute.
    """
    if "," not in index_type:
        return index_type

    kind, _, _ = index_type.partition(",")
    return f"{kind},{tmp_dir / (extract_path.stem + '.nodecache')}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the pre-built POI tile grid from OSM extracts.")
    parser.add_argument("extracts", nargs="+", help="One or more .osm.pbf extract files (e.g. from Geofabrik).")
    parser.add_argument("--out", required=True, help="Directory to write <latIndex>_<lngIndex>.json.gz tiles into.")
    parser.add_argument(
        "--index",
        default="flex_mem",
        help=(
            "osmium location index type used to resolve way/area node coordinates "
            "(list them all with: python -c \"import osmium.index; print(osmium.index.map_types())\"). "
            "The default, flex_mem, keeps everything in memory. For a memory-constrained "
            "environment (e.g. a CI runner with a hard memory limit), pass a file-backed type "
            "instead, e.g. 'sparse_file_array,<anything>' -- the part after the comma "
            "is just a placeholder (see index_arg_for_extract): the kernel can then "
            "reclaim its pages under memory pressure instead of the process being "
            "OOM-killed, at some added disk-I/O cost. See README.md."
        ),
    )
    args = parser.parse_args(argv)

    handler = PoiHandler()

    # A plain mkdtemp (rather than TemporaryDirectory's own context manager)
    # deliberately: a file-backed index's backing file can still be
    # memory-mapped by osmium's C++ side for a moment after apply_file
    # returns, which makes deleting it right away fail on Windows ("used by
    # another process") even though the script itself is done with it --
    # ignore_errors=True below means that leftover, if it happens, is merely
    # a stale temp file for the OS to reclaim later, not a crash.
    tmp_dir = Path(tempfile.mkdtemp(prefix="poi-tiles-idx-"))
    try:
        for extract in args.extracts:
            index_arg = index_arg_for_extract(args.index, Path(extract), tmp_dir)
            print(f"Scanning {extract} (index={index_arg}) ...", file=sys.stderr)
            handler.apply_file(extract, locations=True, idx=index_arg)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    tile_count, total_bytes = write_tiles(handler.tiles, Path(args.out))

    print(f"Total POIs: {handler.total_poi_count()}")
    print("Per-category counts:")
    for category, count in sorted(handler.category_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {category}: {count}")
    print("Per-brand counts (wikidata id):")
    for brand_id, count in sorted(handler.brand_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {brand_id}: {count}")
    print(f"Tiles written: {tile_count}")
    print(f"Total tile bytes: {total_bytes}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
