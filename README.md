# locationnotes-tiles

Builds the pre-built POI tile grid the [Tacky](https://github.com/jkidi/locationnotes) (Notes for Places) app
serves brand/category alerts from, and publishes it as a static site over
GitHub Pages. This repo used to live inside the app repo as
`tool/poi_tiles/`; it moved out on its own so the tile-build pipeline (which
needs to download and process gigabytes of OpenStreetMap data) is decoupled
from the app's own build/release process, and so the tiles can be hosted for
free straight off this repo's Pages site.

## What it builds

- **`build_poi_tiles.py`** -- scans one or more `.osm.pbf` extracts for nodes
  and way/relation areas matching one of 21 curated categories (`shop=*`,
  `amenity=*`, `leisure=fitness_centre` -- see `CURATED_TAGS`, kept in sync by
  hand with the app repo's `lib/poi/poi_category.dart`) and bins them into a
  0.2 degree x 0.2 degree tile grid, writing
  `<out>/<latIndex>_<lngIndex>.json.gz`: a gzip-compressed JSON array of
  `{"osmId", "lat", "lng", "name"?, "brandWikidataId"?, "category"}` objects.
  This is the app's `TilePoiSource`/`PoiTile` tile format, unchanged from
  when this pipeline lived in the app repo. A POI's `brandWikidataId` is set
  straight from its `brand:wikidata` tag whenever that value looks like a
  Wikidata id (`^Q\d+$`) -- this pipeline does not carry or consult the app's
  brand catalog at all any more; validating a brand id against a known list
  is the app's job now, not the tile builder's.
- **`list_extracts.py`** -- reads Geofabrik's `index-v1.json` and expands one
  of the five coverage regions (see `regions.txt`) into the actual leaf
  extracts to download: ordinary countries as-is, but a country whose own
  `.osm.pbf` is bigger than 1 GB (e.g. Germany, France, Poland, Italy, Spain,
  the UK, the US, Canada) is replaced by its own states/voivodeships/provinces
  instead, one extract at a time.
- **`merge_tiles.py`** -- merges two or more tile directories into one,
  deduplicating by `osmId` within a tile. Used both to combine a region's own
  split-up extracts (if that ever produces overlapping tiles) and, in the
  final CI job, to combine all five regions' tile sets into the one that's
  actually deployed -- a POI near a border between two regions/extracts can
  legitimately appear in both, and this is where that gets collapsed back
  down to one copy.
- **`.github/workflows/build-tiles.yml`** -- the pipeline above, run weekly
  (and on manual dispatch): one matrix job per region (download -> pre-filter
  with `osmium tags-filter` -> `build_poi_tiles.py` -> upload as an artifact),
  then one job that downloads every region's artifact, merges them, checks
  the total size, and deploys to GitHub Pages.

## Coverage

Geofabrik regions `europe`, `north-america`, `central-america`,
`south-america`, `australia-oceania` (`regions.txt`) -- no Asia, Africa,
Antarctica, or Russia.

A few small, inhabited territories would otherwise fall through
`list_extracts.py`'s ISO-code-based country selection entirely -- either
because Geofabrik lists them with no `iso3166-1:alpha2` code at all (Kosovo,
the Azores, the Isle of Man, Guernsey/Jersey), or because Geofabrik files
them under a region this pipeline doesn't otherwise build (the Canary
Islands, Spanish territory filed under Geofabrik's "africa"). These are
listed explicitly in `list_extracts.py`'s `EXTRA_EXTRACTS` and appended to
the `europe` region's job. If Geofabrik's index ever adds an ISO code for
one of these, `country_candidates` would pick it up on its own and the
explicit entry becomes redundant (harmless, just remove it if noticed).

## Running locally

### Prerequisites

```
pip install -r requirements.txt
```

A `cp3xx-win_amd64`/manylinux/macOS wheel exists for `osmium` (pyosmium), so
no Docker/WSL detour is needed for `build_poi_tiles.py`, `merge_tiles.py`, or
`list_extracts.py` themselves on any of the three platforms.

`osmium-tool` (the separate CLI, providing the `osmium tags-filter` command
the CI workflow uses to pre-filter an extract before handing it to
`build_poi_tiles.py`) is a different package from pyosmium and has no
official Windows build. On Linux/macOS, install it with your package manager
(e.g. `apt-get install osmium-tool`, `brew install osmium-tool`). On Windows,
run it inside Docker instead, e.g.:

```
docker run --rm -v "%cd%:/data" ubuntu:24.04 bash -c "apt-get update -qq && apt-get install -y -qq osmium-tool && osmium tags-filter --overwrite -o /data/filtered.osm.pbf /data/extract.osm.pbf nwr/shop nwr/amenity nwr/leisure=fitness_centre"
```

Pre-filtering is optional for a local/by-hand run -- `build_poi_tiles.py`
itself already ignores anything that isn't a curated tag -- but it makes a
large extract dramatically faster and lighter to process (see "Measurements"
below), and it's what the CI workflow always does before the Python step.

### Getting a test extract

Small extracts for a quick local run/sanity check (a couple of MB, seconds to
process):

```
curl -sSL -o liechtenstein-latest.osm.pbf https://download.geofabrik.de/europe/liechtenstein-latest.osm.pbf
python build_poi_tiles.py liechtenstein-latest.osm.pbf --out /tmp/poi-tiles-test
```

Full region/country extracts are much larger -- pre-filter them first (see
above) unless you have a specific reason not to.

### Listing a region's extracts

```
python list_extracts.py europe
```

Prints one Geofabrik path per line (e.g. `europe/andorra`,
`europe/poland/mazowieckie`, ... -- no `-latest.osm.pbf` suffix), the same
paths the CI workflow downloads
`https://download.geofabrik.de/<path>-latest.osm.pbf` for.

### Merging tile sets

```
python merge_tiles.py tiles-europe/ tiles-north-america/ ... --out merged-tiles --max-bytes 900000000
```

Exits non-zero (after still writing the merged output) if the result is over
`--max-bytes` -- see "Size limit" below.

## Unit tests

Pure logic tests, no `.osm.pbf` parsing and no real network calls:

```
python -m unittest test_build_poi_tiles.py test_merge_tiles.py test_list_extracts.py
```

## Measurements

Re-measured on `europe/poland/mazowieckie` (the largest Polish voivodeship,
includes Warsaw; 286 MiB / 299,918,654 bytes raw) after moving the pipeline
into this repo and dropping parking, run on the same machine as the app
repo's original phase-5 measurement:

| step | wall time | peak RSS | output |
|---|---|---|---|
| `osmium tags-filter` pre-filter (Docker/ubuntu:24.04) | 27.6 s | -- | 11,459,841 bytes (10.9 MiB, **96.2% smaller** than the raw extract) |
| `build_poi_tiles.py` on the filtered extract, `--index flex_mem` | 28.3 s | 91.5 MB | 27,355 POIs, 149 tiles, 751,014 bytes |
| `build_poi_tiles.py` on the filtered extract, `--index sparse_file_array` (what CI uses) | 29.3 s | 97.8 MB | byte-for-byte identical to the `flex_mem` row |

Total pipeline wall time for this one extract: **~56 s**, vs. phase 5's
original ~21.1 minutes for the same extract (unfiltered, with the app's
brand-catalog lookup, and including parking) -- a ~22x speedup, driven
almost entirely by the pre-filter step cutting ~96% of the extract's bytes
before the Python side ever has to resolve a way/area's node coordinates.
Peak memory dropped from ~736 MB to under 100 MB for the same reason.

**Parking-free POI count**: 27,355, vs. phase 5's 77,768 *including*
parking. The 50,413 difference is consistent with a direct count of
`amenity=parking` elements in the same filtered extract (894 nodes + 49,438
ways = 50,332; the remaining ~80 are `amenity=parking` multipolygon
relations and/or ways whose area centroid couldn't be resolved, neither of
which the quick node/way-only recount above included) -- confirming the
count difference is the parking category being dropped, not a filtering
regression. Tile count dropped from 155 to 149: a handful of Mazowieckie's
tiles apparently held only parking POIs and nothing else, so they vanish
entirely once parking is gone. Tile bytes dropped from 1,882,531 to 751,014
(60.1% smaller), roughly tracking the 64.8% POI-count drop.

If `regions.txt` or `list_extracts.py`'s size threshold ever change enough to
put much bigger single-job extract sets through the pipeline, re-measure
rather than assuming these per-extract numbers scale linearly across a whole
region's job.

## Enabling GitHub Pages

1. In this repo's GitHub settings: **Settings -> Pages -> Source: GitHub
   Actions**. `.github/workflows/build-tiles.yml`'s `merge-and-deploy` job
   already builds and deploys the Pages artifact (`actions/configure-pages`,
   `actions/upload-pages-artifact`, `actions/deploy-pages`) -- nothing else to
   configure once Pages itself is turned on for this repo.
2. The resulting base URL is
   `https://<owner>.github.io/locationnotes-tiles/` (or your fork/org's
   equivalent -- GitHub shows the exact URL under Settings -> Pages once
   it's enabled). Point the app at it:
   ```
   flutter build apk --debug --dart-define=POI_TILE_BASE_URL=https://<owner>.github.io/locationnotes-tiles
   ```
   See the app repo's `lib/poi/poi_source_factory.dart`.
3. Every deploy **replaces the whole site** -- Pages has no notion of
   "only the changed tiles", so each weekly run re-uploads every tile file
   even if most of them are unchanged. That's fine at this pipeline's size
   (well under Pages' 1 GB soft site-size guidance and 100 GB/month
   bandwidth guidance for a public repo), but it's the reason a future move
   to incremental/only-changed-tiles publishing would need a different host
   (see "R2 fallback" below) -- it isn't something Pages itself can do.

### Size limit

`merge_tiles.py`'s `--max-bytes` (default 900,000,000 bytes = 900 MB, and
what the CI workflow passes explicitly) fails the merge-and-deploy job
*before* it reaches `upload-pages-artifact`/`deploy-pages` if the merged tile
set is over that -- comfortably under GitHub Pages' documented ~1 GB
recommended site-size limit, leaving headroom for the site to grow before
actually hitting that ceiling.

#### If the size check fails

Drop a region from **both** `build-tiles.yml`'s matrix
(`jobs.build-region.strategy.matrix.region`) and `regions.txt`, rather than
raising `--max-bytes` past what Pages can reasonably serve. The workflow's
matrix is what CI actually iterates over; `regions.txt` on its own is a
human-readable list of the same regions (read by this README and by code
comments, not by the workflow), so removing a region from `regions.txt`
alone would not stop that region's job from running. Lowering
`list_extracts.py`'s `--size-threshold-bytes` doesn't help here either -- it
only changes how finely an already-included country gets split into smaller
extracts, not how much total data the pipeline processes.

### R2 fallback

If the tile set ever needs to grow past what GitHub Pages can comfortably
serve (size, bandwidth, or wanting only-changed-tiles incremental publishing
instead of a full-site replace every run), the fallback is Cloudflare R2:
create a bucket, set it up for public read access (or front it with a custom
domain), add `CF_ACCOUNT_ID` / `CF_R2_ACCESS_KEY_ID` /
`CF_R2_SECRET_ACCESS_KEY` / `CF_R2_BUCKET` as secrets on this repo, and
replace `merge-and-deploy`'s last three steps
(`configure-pages`/`upload-pages-artifact`/`deploy-pages`) with an upload
step such as:

```yaml
- name: Upload to R2
  uses: shallwefootball/s3-upload-action@v1.3.3
  with:
    aws_key_id: ${{ secrets.CF_R2_ACCESS_KEY_ID }}
    aws_secret_access_key: ${{ secrets.CF_R2_SECRET_ACCESS_KEY }}
    aws_bucket: ${{ secrets.CF_R2_BUCKET }}
    source_dir: merged-tiles
    endpoint: https://${{ secrets.CF_ACCOUNT_ID }}.r2.cloudflarestorage.com
```

Then point `POI_TILE_BASE_URL` at the bucket's public URL/custom domain
instead of the Pages URL. Nothing in `build_poi_tiles.py`/`merge_tiles.py`
needs to change either way -- both just produce a directory of
`<latIndex>_<lngIndex>.json.gz` files; only where that directory ends up
served from differs.

## Adding a region

Append a Geofabrik top-level region id (e.g. `asia`) to `regions.txt` and add
it to `build-tiles.yml`'s matrix. `list_extracts.py` figures out that
region's actual leaf extracts (and any size-based country splitting) on its
own at build time -- nothing else needs to change by hand.

## License

The code in this repository (`build_poi_tiles.py`, `merge_tiles.py`,
`list_extracts.py`, their tests, and the workflow) is MIT-licensed -- see
`LICENSE`.

The map data this pipeline downloads, filters, and republishes as tiles is
**not** covered by that license: it comes from
[OpenStreetMap](https://www.openstreetmap.org/copyright) via
[Geofabrik](https://download.geofabrik.de/), and remains licensed under the
**Open Database License (ODbL) 1.0**. Anything built from OSM data --
including the tiles this pipeline publishes -- must carry OSM's attribution
("© OpenStreetMap contributors") and comply with ODbL's share-alike terms.
The app displays that attribution itself (in its About screen, under the
brand picker's results, and under the category dropdown in the alert
editor); this repo's own README carries it here for the tiles it hosts
directly.
