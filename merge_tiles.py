#!/usr/bin/env python3
"""Merges two or more tile directories (each produced by build_poi_tiles.py,
or by an earlier merge_tiles.py run) into one, deduplicating by `osmId`
within each tile file.

Usage:
    python merge_tiles.py <dir1> <dir2> [<dir3> ...] --out <merged_dir> \
        [--max-bytes N]

Used at two points in the CI pipeline (.github/workflows/build-tiles.yml):

- Within a region's build job, when that region's coverage is split across
  several Geofabrik extracts (see list_extracts.py) whose coverage may
  overlap at the edges (e.g. two neighbouring voivodeships/states each
  including a sliver of the other for context) -- though in practice this
  case doesn't arise here, since build_poi_tiles.py itself already merges
  multiple extracts passed in one invocation.
- In the final merge job, combining every region's tile directory (europe,
  north-america, ...) into the one tile set actually deployed. Geofabrik's
  regions can overlap at their borders (e.g. a POI near the Poland/Germany
  border can appear in both the europe/poland and europe/germany extracts,
  which both get processed under the "europe" region job here), so the same
  real-world POI can arrive with the same `osmId` from more than one input
  directory. This is also true of a POI that lands in a single tile
  (0.2 degree square) that Geofabrik happens to split between two regions
  entirely (e.g. a tile straddling two continents' extract boundaries) --
  the same tile *file* (`<latIndex>_<lngIndex>.json.gz`) can exist in more
  than one input directory, each holding a different subset of that tile's
  POIs, or the same POI more than once. Either way, deduplication is by
  `osmId` within a tile, exactly as build_poi_tiles.py already does when
  merging multiple extracts in one run -- so a border POI appears exactly
  once in the merged output regardless of how many input directories it came
  from.

Later input directories win when the same (tile, osmId) pair is present in
more than one -- in practice this doesn't matter, since the same real-world
element's tags (and therefore its POI JSON) don't depend on which extract it
was read from, but a deterministic tie-break (rather than "undefined") is
one less thing to wonder about when debugging.

Exits non-zero (after still writing the merged output) when the merged tile
set's total size exceeds `--max-bytes` (default 900,000,000 bytes = 900 MB,
global-constraints.md "Tile hosting": GitHub Pages' recommended limit) --
this is meant to fail the CI job's merge step before it reaches
`upload-pages-artifact`/`deploy-pages`, not to silently publish an
oversized site. See README.md "If the size check fails" for what to do then
(shrink `regions.txt`'s coverage).
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from pathlib import Path

import build_poi_tiles as bpt

DEFAULT_MAX_BYTES = 900_000_000

_TILE_FILE_NAME_RE = re.compile(r"^(-?\d+)_(-?\d+)\.json\.gz$")


def tile_key_from_file_name(file_name: str) -> tuple[int, int]:
    """(latIndex, lngIndex) parsed back out of a `<latIndex>_<lngIndex>.json.gz`
    file name (the inverse of build_poi_tiles.tile_file_name). Raises
    ValueError for any file name that doesn't match that shape -- callers only
    ever pass in names already filtered by a `*.json.gz` glob, but a directory
    with an unexpected file in it should fail loudly rather than silently
    dropping or misfiling it."""
    match = _TILE_FILE_NAME_RE.match(file_name)
    if match is None:
        raise ValueError(f"not a tile file name: {file_name!r}")
    return int(match.group(1)), int(match.group(2))


def merge_directories(dirs: list[Path]) -> dict[tuple[int, int], dict[str, dict]]:
    """Merges every `*.json.gz` tile file across [dirs] into one
    `{(latIndex, lngIndex): {osmId: poi}}` structure, deduplicated by `osmId`
    within each tile -- see the module docstring for why a tile or an `osmId`
    within it can legitimately appear in more than one input directory."""
    tiles: dict[tuple[int, int], dict[str, dict]] = {}

    for directory in dirs:
        for path in sorted(directory.glob("*.json.gz")):
            tile_key = tile_key_from_file_name(path.name)
            pois = json.loads(gzip.decompress(path.read_bytes()))

            bucket = tiles.setdefault(tile_key, {})
            for poi in pois:
                bucket[poi["osmId"]] = poi

    return tiles


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge tile directories, deduplicating by osmId within a tile.")
    parser.add_argument("dirs", nargs="+", help="Two or more tile directories to merge.")
    parser.add_argument("--out", required=True, help="Directory to write the merged <latIndex>_<lngIndex>.json.gz tiles into.")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"Fail (after writing the merged output) if total tile bytes exceed this (default {DEFAULT_MAX_BYTES}).",
    )
    args = parser.parse_args(argv)

    dirs = [Path(d) for d in args.dirs]
    tiles = merge_directories(dirs)
    tile_count, total_bytes = bpt.write_tiles(tiles, Path(args.out))

    print(f"Merged {len(dirs)} directories into {tile_count} tiles, {total_bytes} bytes total.")

    if total_bytes > args.max_bytes:
        print(
            f"ERROR: merged tile set is {total_bytes} bytes, over the {args.max_bytes}-byte limit "
            "(global-constraints.md \"Tile hosting\"). Shrink regions.txt's coverage before this "
            "can be deployed -- see README.md \"If the size check fails\".",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
