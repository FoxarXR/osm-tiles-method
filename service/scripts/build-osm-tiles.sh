#!/bin/bash
# Build the OpenStreetMap detail's two archives (SPEC.md section 32, INSTALL.md): osm-detail-<build>-v1
# (z9 to z14) and osm-buildings-<build>-v1 (z15), from a daily Protomaps basemap build. The players ask
# for osm-detail-latest and osm-buildings-latest: once the two are in the bucket, the aliases move to
# them in service/src/tilesets.ts and a deploy of the service serves them.
#
#   service/scripts/build-osm-tiles.sh <work folder> <tilesets folder> [step...]
#
# Steps, all of them in this order by default:
#   fetch      the build's z9 to z15 over the profile's box into the work folder (`pmtiles extract`)
#   detail     the detail archive into the tilesets folder (build-osm-tiles.py detail)
#   buildings  the buildings archive (build-osm-tiles.py buildings)
#   verify     `pmtiles verify`, tiles and bytes per zoom, the attribution files
# PROFILE: world (the default, the published names), france or dijon (named -france-, -dijon-: a
# measure, never published). BUILD: a build of build.protomaps.com, as 20261016 (default: the
# latest). PYTHON_IMAGE: the interpreter (default pypy:3.10-slim; python:3.12-slim works too).
# WORKERS: the processes (default: the cores).
set -euo pipefail

if [ $# -lt 2 ]; then
	sed -n '2,18p' "$0"
	exit 1
fi
mkdir -p "$1" "$2"
WORK=$(cd "$1" && pwd)
TILESETS=$(cd "$2" && pwd)
shift 2
STEPS=("$@")
[ ${#STEPS[@]} -eq 0 ] && STEPS=(fetch detail buildings verify)
HERE=$(cd "$(dirname "$0")" && pwd)
STYLE=$(cd "$HERE/../../style" && pwd)
PROFILE="${PROFILE:-world}"
PMTILES_IMAGE=protomaps/go-pmtiles:latest
PYTHON_IMAGE="${PYTHON_IMAGE:-pypy:3.10-slim}"
WORKERS="${WORKERS:-$(getconf _NPROCESSORS_ONLN)}"
case $PROFILE in
world) BOX="" ; SUFFIX="" ;;
# Metropolitan France and Corsica, with a margin of its neighbours.
france) BOX="-5.5,41.2,9.8,51.3" ; SUFFIX="-france" ;;
dijon) BOX="4.95,47.28,5.12,47.36" ; SUFFIX="-dijon" ;;
*) echo "unknown profile: $PROFILE" >&2; exit 1 ;;
esac
if [ -z "${BUILD:-}" ]; then
	BUILD=$(curl -sSf https://build-metadata.protomaps.dev/builds.json | python3 -c 'import json, sys; print(json.load(sys.stdin)[-1]["key"].split(".")[0])')
fi
DETAIL="osm-detail-${BUILD}${SUFFIX}-v1"
BUILDINGS="osm-buildings-${BUILD}${SUFFIX}-v1"
SOURCE="protomaps-$BUILD-$PROFILE.pmtiles"

log() {
	echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') $*" | tee -a "$WORK/build.log"
}

bake() {
	# A bake that outgrows the machine stops instead of stalling it (16/10/2026: 125 GB full, the
	# machine unreachable for two hours).
	local limit=()
	[ -r /proc/meminfo ] && limit=(--memory="$(awk '/MemTotal/ {printf "%dk", $2 * 0.85}' /proc/meminfo)")
	docker run --rm "${limit[@]}" -v "$WORK":/work -v "$TILESETS":/tilesets -v "$HERE":/scripts:ro -v "$STYLE":/style:ro \
		"$PYTHON_IMAGE" sh -c 'exec "$(command -v pypy3 || command -v python3)" "$@"' bake /scripts/build-osm-tiles.py "$@"
}

fetch() {
	[ -e "$WORK/$SOURCE" ] && { log "$SOURCE is there"; return; }
	# Into a partial name first: a fetch cut short (a connection) leaves no archive behind.
	if [ -z "$BOX" ]; then
		# The world: the build whole (139 GB in October 2026) is faster than an extract tile by tile,
		# and z0 to z8 are a sliver of it. One connection held 36 MB/s and was cut after 24 GB (an
		# HTTP/2 error curl does not retry): aria2c's 16 resume on their own; else curl, resumed.
		if command -v aria2c >/dev/null; then
			aria2c -q -x 16 -s 16 -k 64M --max-tries=0 --retry-wait=5 --file-allocation=none \
				-d "$WORK" -o "$SOURCE.part" "https://build.protomaps.com/$BUILD.pmtiles"
		else
			until curl -sSfL --http1.1 -C - -o "$WORK/$SOURCE.part" "https://build.protomaps.com/$BUILD.pmtiles"; do
				log "fetch cut short at $(du -h "$WORK/$SOURCE.part" | cut -f1), resumed"
				sleep 5
			done
		fi
	else
		docker run --rm -v "$WORK":/work "$PMTILES_IMAGE" extract "https://build.protomaps.com/$BUILD.pmtiles" \
			"/work/$SOURCE.part" --minzoom=9 --maxzoom=15 --download-threads=16 --bbox="$BOX"
	fi
	mv "$WORK/$SOURCE.part" "$WORK/$SOURCE"
	log "Protomaps basemap $BUILD, $PROFILE: $(du -h "$WORK/$SOURCE" | cut -f1)"
}

detail() {
	bake detail "/work/$SOURCE" "/tilesets/$DETAIL.pmtiles" /style/osm-style.json --workers "$WORKERS" --build "$BUILD" |
		tee -a "$WORK/build.log"
}

buildings() {
	bake buildings "/work/$SOURCE" "/tilesets/$BUILDINGS.pmtiles" --workers "$WORKERS" --build "$BUILD" |
		tee -a "$WORK/build.log"
}

verify() {
	local name
	for name in "$DETAIL" "$BUILDINGS"; do
		docker run --rm -v "$TILESETS":/tilesets "$PMTILES_IMAGE" verify "/tilesets/$name.pmtiles"
		bake stats "/tilesets/$name.pmtiles" | tee -a "$WORK/build.log"
	done
	cat >"$TILESETS/$DETAIL.attribution.txt" <<EOF
$DETAIL: roads, railways, water, land uses, useful places (a family's kinds, named), towns and
neighbourhoods, Mapbox Vector Tiles z9 to z14, gzipped; each z14 tile holds its z15 children's useful
places. Layers roads, water, landuse, pois, places. foxar-geo SPEC.md section 32.
EOF
	cat >"$TILESETS/$BUILDINGS.attribution.txt" <<EOF
$BUILDINGS: the buildings' polygons and heights, Mapbox Vector Tiles z15, gzipped, one layer
buildings. foxar-geo SPEC.md section 32.
EOF
	local name
	for name in "$DETAIL" "$BUILDINGS"; do
		cat >>"$TILESETS/$name.attribution.txt" <<EOF
© OpenStreetMap contributors. The data is available under the Open Database License (ODbL 1.0,
https://opendatacommons.org/licenses/odbl/1-0/); this archive is a Derivative Database under it,
made from the Protomaps basemap build $BUILD (https://build.protomaps.com/$BUILD.pmtiles) by
service/scripts/build-osm-tiles.py, the method of making it.
EOF
	done
}

for step in "${STEPS[@]}"; do
	log "$step: start ($PROFILE, build $BUILD)"
	start=$(date +%s)
	case $step in
	fetch | detail | buildings | verify) $step ;;
	*) echo "unknown step: $step" >&2; exit 1 ;;
	esac
	log "$step: done in $(($(date +%s) - start)) s"
done
