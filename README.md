# Foxar OpenStreetMap tiles: the method

Foxar's 3D maps (GeoMap) draw OpenStreetMap detail past zoom 9: roads, railways, water, land uses,
buildings, the names of streets and neighbourhoods, and useful places such as schools, museums and
stations. They read two tile archives that Foxar derives from the
[Protomaps basemap](https://docs.protomaps.com/basemaps/downloads), itself made from OpenStreetMap
and Natural Earth.

Those archives are a Derivative Database of OpenStreetMap under the
[Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/) (section 4.6). This
repository offers the method that makes them, in machine-readable form: with it and the source build
named below, anyone can make the same archives again.

Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), available under the
ODbL.

## The archives in service

| Archive | Zooms | Source | Served since |
|---|---|---|---|
| `osm-detail-20261016-v1` | 9 to 14 | Protomaps basemap build `20261016` | 16 October 2026 |
| `osm-buildings-20261016-v1` | 15 | Protomaps basemap build `20261016` | 16 October 2026 |

The source build is `https://build.protomaps.com/20261016.pmtiles` (Protomaps basemap schema 4.15.2).
This table changes each time the archives are made again, about every three months; the files in this
repository are always the ones that made the archives listed here.

## What differs from the source

`service/scripts/build-osm-tiles.py` reads the source build and writes the two archives. Everything
it changes is in that file and in `style/osm-style.json`; in short:

**Detail archive, zooms 9 to 14**

- Layers kept: `roads`, `water`, `landuse`, `pois`, `places`. The others are left out.
- Roads: sidewalks, crossings and tunnels are left out.
- Water: underground lines (`layer` below 0) are left out.
- Places of interest: only the kinds of the families in `style/osm-style.json` (`poiFamilies`:
  education, culture, heritage, worship, transport, health, civic, leisure), and only named ones.
- Places: towns and neighbourhoods only (`locality`, `neighbourhood`).
- Properties kept: `kind`, `kind_detail`, `min_zoom`, `name` and its translations (`name:*`),
  `population_rank`, `wikidata`, `layer`, `service`.
- Each zoom 14 tile also holds the useful places of its four zoom 15 children (the source keeps about
  half of them for zoom 15 only), each once by kind and name.

**Buildings archive, zoom 15**

- The `buildings` layer alone: the polygons with `kind`, `height` and `min_height`; names left out.

**Both**

- PMTiles v3, gzipped Mapbox Vector Tiles. Identical small tiles (the sea, the inside of a forest)
  are stored once.
- Nothing is added that is not in the source.

## Making the archives

Docker and a few hundred GB of disk for the world (the source build was 139 GB in October 2026).

```sh
# The world, from a given build (the default is the latest one):
BUILD=20261016 service/scripts/build-osm-tiles.sh work tilesets

# A small extract to try it (central Dijon, named -dijon-):
PROFILE=dijon BUILD=20261016 service/scripts/build-osm-tiles.sh work tilesets
```

The steps are `fetch`, `detail`, `buildings` and `verify`; `verify` also writes an attribution file
next to each archive. The script only uses Python's standard library (PyPy by default) and the
`protomaps/go-pmtiles` image. The tests: `python3 service/scripts/test_build_osm_tiles.py`.

## Where these files come from

They are copied unchanged from Foxar's map library, at the version that made the archives above
(foxar-geo v0.37.0, commit `26847a8`). The paths are kept so the script finds the style file.

## Licences

- The data in the archives: © OpenStreetMap contributors, ODbL 1.0.
- The files of this repository: MIT, see `LICENSE`.
