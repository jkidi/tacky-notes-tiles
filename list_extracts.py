#!/usr/bin/env python3
"""Lists the Geofabrik leaf extracts to build tiles from, for one of the
coverage regions in regions.txt (europe, north-america, central-america,
south-america, australia-oceania -- see README.md "Coverage").

Usage:
    python list_extracts.py <region> [--index-url URL] [--size-threshold-bytes N]

Reads Geofabrik's index-v1(-nogeom).json (default:
https://download.geofabrik.de/index-v1-nogeom.json) and prints one Geofabrik
extract path per line to stdout (e.g. "europe/andorra", no "-latest.osm.pbf"
suffix -- the same format regions.txt itself uses, and what
build-tiles.yml's per-region job downloads
`https://download.geofabrik.de/<path>-latest.osm.pbf` for).

Selection rule
--------------

A direct child of [region] is a candidate when it has an ISO 3166-1 alpha-2
code (`iso3166-1:alpha2` in the index) and its own id has no `/` in it. That
excludes both:

- Geofabrik's overlapping multi-country convenience extracts (e.g. "dach",
  "alps", "britain-and-ireland", "us-midwest") -- these lack an ISO code
  entirely, since they aren't a single country.
- A country's own subdivisions when the index happens to list them as direct
  children of the continent rather than of the country (Geofabrik does this
  for the US: "us/california"'s "parent" is "north-america", not "us", even
  though its id already encodes the "us/" prefix) -- these still carry an ISO
  code (a state-level ISO 3166-2 code, in the US's case) but their id
  contains a "/", which their true parent country's plain id never does.

Each candidate country is listed as-is UNLESS its own `.osm.pbf` extract is
larger than [size_threshold_bytes] (default 1 GB: HTTP HEAD's `Content-Length`
on its `urls.pbf`) -- Poland's ~1.9 GB whole-country extract, for instance,
turns a single ~740 MB-memory, ~21-minute build (see the app repo's phase 5
measurement, before this pipeline moved into its own repo) into a much
riskier multi-hour, multi-GB one. When a country is over the threshold, its
own children take its place, found the same way regardless of which
Geofabrik nesting convention that country uses: every entry whose "parent" is
this country's id, unioned with every entry whose own id starts with
"<country id>/" (see `children_of`). A country over the threshold with no
children found for it either way is kept whole, with a warning on stderr --
there is nothing finer-grained to substitute.

This rule is deliberately just one level deep (country -> its direct
children only, never grandchildren): every region this pipeline covers
bottoms out there already (no country here has a > 1 GB subdivision as of
this writing) and re-measuring recursively adds a lot of complexity for a
case that doesn't currently exist. If that ever changes, re-derive/extend
this rather than assuming a single level is still enough.

Territories the selection rule above misses entirely
------------------------------------------------------

A handful of real, inhabited territories have no `iso3166-1:alpha2` code in
Geofabrik's index at all (Kosovo, the Azores, the Isle of Man,
Guernsey/Jersey), so `country_candidates` never picks them up no matter which
region they're nested under. The Canary Islands are Spanish territory but
Geofabrik files them as a child of "africa", not "europe", so even a
hypothetical ISO-code-based match wouldn't place them under the "europe"
region job. `EXTRA_EXTRACTS` below lists these explicitly, by their exact
Geofabrik path, so `main()` can append them to a region's selection
regardless of the ISO-code rule -- see `README.md` "Coverage" for the
reasoning kept in sync with this list.
"""

from __future__ import annotations

import argparse
import sys
from typing import Callable

import requests

DEFAULT_INDEX_URL = "https://download.geofabrik.de/index-v1-nogeom.json"
DEFAULT_SIZE_THRESHOLD_BYTES = 1_000_000_000
REQUEST_TIMEOUT_SECONDS = 30

PropsById = dict[str, dict]

# Extra Geofabrik paths to include for a region beyond what the ISO-code-based
# `country_candidates` rule can ever find on its own (see the module
# docstring's "Territories the selection rule above misses entirely"). Listed
# by exact Geofabrik path (not id), since `region_path` needs a feature's
# `urls.pbf` to derive a path and these features either have no ISO code to
# match on, or (Canary Islands) sit under a different top-level region than
# the one whose coverage they belong to.
EXTRA_EXTRACTS: dict[str, list[str]] = {
    "europe": [
        "europe/kosovo",
        "europe/azores",
        "europe/isle-of-man",
        "europe/guernsey-jersey",
        "africa/canary-islands",
    ],
}


def index_by_id(index_json: dict) -> PropsById:
    """`{id: properties}` for every feature in a parsed index-v1(-nogeom).json
    document."""
    return {feature["properties"]["id"]: feature["properties"] for feature in index_json["features"]}


def region_path(props: dict) -> str:
    """The Geofabrik path (e.g. "europe/poland/mazowieckie") for a feature's
    properties, derived from its own `urls.pbf` download URL rather than by
    walking `parent` links -- the two nesting conventions `children_of`
    documents mean a naive parent-chain join would double up the "us/" prefix
    for a US state (e.g. "us/california"'s parent is "north-america", not
    "us"). The URL's path between the domain and the "-latest.osm.pbf" suffix
    is exactly this pipeline's/regions.txt's path format already, for every
    entry, top-level region or leaf extract alike."""
    pbf_url = props["urls"]["pbf"].removesuffix("-latest.osm.pbf")
    after_domain = pbf_url.split("://", 1)[-1].split("/", 1)[1]
    return after_domain


def country_candidates(region: str, props_by_id: PropsById) -> list[str]:
    """Direct children of [region] that look like a single real country --
    see the module docstring's "Selection rule"."""
    return [
        id_
        for id_, props in props_by_id.items()
        if props.get("parent") == region and props.get("iso3166-1:alpha2") and "/" not in id_
    ]


def children_of(country_id: str, props_by_id: PropsById) -> list[str]:
    """Every entry that's a subdivision of [country_id], under either
    Geofabrik nesting convention (see the module docstring)."""
    by_parent = {id_ for id_, props in props_by_id.items() if props.get("parent") == country_id}
    by_id_prefix = {id_ for id_ in props_by_id if id_.startswith(country_id + "/")}
    return sorted(by_parent | by_id_prefix)


def select_extracts(
    region: str,
    props_by_id: PropsById,
    get_size_bytes: Callable[[dict], int],
    size_threshold_bytes: int = DEFAULT_SIZE_THRESHOLD_BYTES,
    extra_extracts: dict[str, list[str]] | None = None,
) -> list[str]:
    """The list of Geofabrik paths to build tiles from for [region] -- see the
    module docstring's "Selection rule". [get_size_bytes] takes a country's
    properties and returns its `.osm.pbf` extract's size in bytes; injected so
    tests never make a real network call. [extra_extracts] (keyed by region,
    see `EXTRA_EXTRACTS`) is appended verbatim -- omitted (the default) so
    existing callers/tests that only care about the ISO-code-based rule are
    unaffected; `main()` passes the real `EXTRA_EXTRACTS`."""
    paths: list[str] = []

    for country_id in sorted(country_candidates(region, props_by_id)):
        props = props_by_id[country_id]
        size_bytes = get_size_bytes(props)

        if size_bytes <= size_threshold_bytes:
            paths.append(region_path(props))
            continue

        children = children_of(country_id, props_by_id)
        if not children:
            print(
                f"WARNING: {country_id} is {size_bytes} bytes (over the {size_threshold_bytes}-byte "
                "threshold) but has no listed subdivisions -- keeping it whole.",
                file=sys.stderr,
            )
            paths.append(region_path(props))
            continue

        paths.extend(region_path(props_by_id[child_id]) for child_id in children)

    paths.extend((extra_extracts or {}).get(region, []))

    return paths


def fetch_index(index_url: str) -> dict:
    response = requests.get(index_url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def http_head_content_length(props: dict) -> int:
    """A real `get_size_bytes` for `select_extracts`: HTTP HEAD's
    `Content-Length` for a country's `urls.pbf` (no body downloaded, just to
    learn its size)."""
    response = requests.head(props["urls"]["pbf"], timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
    response.raise_for_status()
    return int(response.headers["Content-Length"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("region", help="A top-level Geofabrik region, e.g. europe (see regions.txt).")
    parser.add_argument("--index-url", default=DEFAULT_INDEX_URL, help=f"Default: {DEFAULT_INDEX_URL}")
    parser.add_argument(
        "--size-threshold-bytes",
        type=int,
        default=DEFAULT_SIZE_THRESHOLD_BYTES,
        help=f"A country extract over this size is replaced by its own children (default {DEFAULT_SIZE_THRESHOLD_BYTES}).",
    )
    args = parser.parse_args(argv)

    props_by_id = index_by_id(fetch_index(args.index_url))
    if args.region not in props_by_id:
        print(f"ERROR: {args.region!r} is not a known Geofabrik region id.", file=sys.stderr)
        return 1

    for path in select_extracts(
        args.region, props_by_id, http_head_content_length, args.size_threshold_bytes, EXTRA_EXTRACTS
    ):
        print(path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
