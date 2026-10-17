#!/usr/bin/env python3
"""The OpenStreetMap detail's two archives from a Protomaps basemap extract (SPEC.md section 32).

    build-osm-tiles.py detail <source.pmtiles> <out.pmtiles> <osm-style.json> [--workers N] [--build DATE]
    build-osm-tiles.py buildings <source.pmtiles> <out.pmtiles> [--workers N] [--build DATE]
    build-osm-tiles.py stats <archive.pmtiles>

`detail` writes z9 to z14: the layers the players draw (roads, water, landuse, pois, places), the
features they draw (no sidewalks, crossings, tunnels or underground water; the useful places of a
family only, named; towns and neighbourhoods), the properties they read. Protomaps keeps half the
useful places for z15 (a school, a theatre): each z14 tile takes its four z15 children's, so a
player reading z14 past it shows them all. `buildings` writes z15: the buildings' polygons alone,
their heights kept. Both write PMTiles v3 directly, gzipped MVT, a run for repeated tiles and one
copy of each small tile (the sea, a forest's inside): a world's sea would be hundreds of millions
of rows in an MBTiles. Python's standard library only, so it runs in any image.
"""

import argparse
import bisect
import collections
import gzip
import hashlib
import json
import mmap
import multiprocessing
import os
import struct
import sys
import tempfile
import time
from array import array
from functools import lru_cache

DETAIL_LAYERS = ('roads', 'water', 'landuse', 'pois', 'places')
KEPT_KEYS = {'kind', 'kind_detail', 'min_zoom', 'name', 'population_rank', 'wikidata', 'layer', 'service'}
BUILDING_KEYS = {'kind', 'height', 'min_height'}
DROPPED_ROADS = {'sidewalk', 'crossing'}
PLACE_KINDS = {'locality', 'neighbourhood'}
DETAIL_ZOOMS = range(9, 15)
BUILDINGS_ZOOM = 15
# A tile this small (gzipped) is repeated as one copy (the sea, a forest's inside).
SHARED_BELOW = 1024

# --- Varints and protobuf fields -------------------------------------------------------------


def varint(b, i):
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def put_varint(out, v):
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)


def fields(b):
    i, n = 0, len(b)
    while i < n:
        k, i = varint(b, i)
        f, t = k >> 3, k & 7
        if t == 0:
            v, i = varint(b, i)
        elif t == 2:
            ln, i = varint(b, i)
            v = b[i:i + ln]
            i += ln
        elif t == 5:
            v = b[i:i + 4]
            i += 4
        elif t == 1:
            v = b[i:i + 8]
            i += 8
        else:
            raise ValueError(f'wire type {t}')
        yield f, t, v


def put_bytes(out, field, data):
    put_varint(out, field << 3 | 2)
    put_varint(out, len(data))
    out += data


def put_uint(out, field, v):
    put_varint(out, field << 3)
    put_varint(out, v)


def packed(b):
    out, i = [], 0
    while i < len(b):
        v, i = varint(b, i)
        out.append(v)
    return out


def put_packed(out, field, values):
    body = bytearray()
    for v in values:
        put_varint(body, v)
    put_bytes(out, field, body)


# --- Mapbox Vector Tiles, at the feature level -------------------------------------------------


def value_of(raw):
    for f, _, v in fields(raw):
        if f == 1:
            return v.decode('utf-8', 'replace')
        if f == 2:
            return struct.unpack('<f', v)[0]
        if f == 3:
            return struct.unpack('<d', v)[0]
        if f in (4, 5):
            return v
        if f == 6:
            return (v >> 1) ^ -(v & 1)
        if f == 7:
            return bool(v)
    return None


class Feature:
    __slots__ = ('id', 'type', 'geometry', 'props', 'raw_values')

    def __init__(self, fid, gtype, geometry, props, raw_values):
        self.id, self.type, self.geometry, self.props, self.raw_values = fid, gtype, geometry, props, raw_values


def decode(tile):
    """{layer name: (extent, [Feature])}; a property's value kept as its raw bytes too."""
    out = {}
    for f, _, v in fields(tile):
        if f != 3:
            continue
        name, extent, keys, vals, feats = None, 4096, [], [], []
        for lf, _, lv in fields(v):
            if lf == 1:
                name = lv.decode()
            elif lf == 5:
                extent = lv
            elif lf == 3:
                keys.append(lv.decode())
            elif lf == 4:
                vals.append(bytes(lv))
            elif lf == 2:
                feats.append(lv)
        features = []
        for fb in feats:
            fid, gtype, geometry, tags = None, 0, b'', []
            for ff, _, fv in fields(fb):
                if ff == 1:
                    fid = fv
                elif ff == 2:
                    tags = packed(fv)
                elif ff == 3:
                    gtype = fv
                elif ff == 4:
                    geometry = bytes(fv)
            props, raws = {}, {}
            for k in range(0, len(tags) - 1, 2):
                key, raw = keys[tags[k]], vals[tags[k + 1]]
                props[key] = value_of(raw)
                raws[key] = raw
            features.append(Feature(fid, gtype, geometry, props, raws))
        out[name] = (extent, features)
    return out


def encode(layers):
    """A tile of `layers` ([(name, extent, [Feature])]), its key and value tables rebuilt."""
    tile = bytearray()
    for name, extent, features in layers:
        if not features:
            continue
        keys, vals, body = {}, {}, bytearray()
        put_uint(body, 15, 2)
        put_bytes(body, 1, name.encode())
        for feature in features:
            tags = []
            for key, raw in feature.raw_values.items():
                tags.append(keys.setdefault(key, len(keys)))
                tags.append(vals.setdefault(raw, len(vals)))
            fb = bytearray()
            if feature.id is not None:
                put_uint(fb, 1, feature.id)
            if tags:
                put_packed(fb, 2, tags)
            put_uint(fb, 3, feature.type)
            put_bytes(fb, 4, feature.geometry)
            put_bytes(body, 2, fb)
        for key in keys:
            put_bytes(body, 3, key.encode())
        for raw in vals:
            put_bytes(body, 4, raw)
        put_uint(body, 5, extent)
        put_bytes(tile, 3, body)
    return bytes(tile)


def kept(feature, keys):
    feature.raw_values = {k: v for k, v in feature.raw_values.items() if k in keys or k.startswith('name:')}
    return feature


def named(feature):
    return 'name' in feature.props or any(k.startswith('name:') for k in feature.props)


def points(geometry):
    """The points of a point geometry (MoveTo, then zigzag deltas)."""
    values, out, x, y, i = packed(geometry), [], 0, 0, 0
    while i < len(values):
        command, count = values[i] & 7, values[i] >> 3
        i += 1
        if command != 1:
            return []
        for _ in range(count):
            dx, dy = values[i], values[i + 1]
            i += 2
            x += (dx >> 1) ^ -(dx & 1)
            y += (dy >> 1) ^ -(dy & 1)
            out.append((x, y))
    return out


def point_geometry(x, y):
    out = bytearray()
    for v in (1 << 3 | 1, (x << 1) ^ (x >> 31), (y << 1) ^ (y >> 31)):
        put_varint(out, v)
    return bytes(out)


# --- The filters --------------------------------------------------------------------------------


def detail_layers(layers, family_kinds):
    out = []
    for name in DETAIL_LAYERS:
        if name not in layers:
            continue
        extent, features = layers[name]
        chosen = []
        for f in features:
            p = f.props
            if name == 'roads' and (p.get('kind_detail') in DROPPED_ROADS or p.get('is_tunnel') is True):
                continue
            if name == 'water' and f.type == 2 and float(p.get('layer') or 0) < 0:
                continue
            if name == 'pois' and (p.get('kind') not in family_kinds or not named(f)):
                continue
            if name == 'places' and p.get('kind') not in PLACE_KINDS:
                continue
            chosen.append(kept(f, KEPT_KEYS))
        out.append((name, extent, chosen))
    return out


def lifted_pois(children, extent, family_kinds, present):
    """The useful places of a z14 tile's z15 children ([(dx, dy, tile bytes)]) in its coordinates,
    the ones it has (`present`: kind and name) left out."""
    out = []
    for dx, dy, tile in children:
        layer = decode(tile).get('pois')
        if not layer:
            continue
        child_extent, features = layer
        for f in features:
            p = f.props
            if p.get('kind') not in family_kinds or not named(f) or f.type != 1:
                continue
            key = (p.get('kind'), p.get('name'))
            pts = [(x, y) for x, y in points(f.geometry) if 0 <= x < child_extent and 0 <= y < child_extent]
            if key in present or not pts:
                continue
            present.add(key)
            x, y = pts[0]
            # The child's quarter of its parent, at the parent's extent.
            px = round((dx * child_extent + x) * extent / (2 * child_extent))
            py = round((dy * child_extent + y) * extent / (2 * child_extent))
            f.geometry = point_geometry(px, py)
            f.id = None
            out.append(kept(f, KEPT_KEYS))
    return out


def building_layers(layers):
    if 'buildings' not in layers:
        return []
    extent, features = layers['buildings']
    chosen = [kept(f, BUILDING_KEYS) for f in features if f.type == 3]
    for f in chosen:
        f.raw_values = {k: v for k, v in f.raw_values.items() if not k.startswith('name:')}
    return [('buildings', extent, chosen)]


# --- PMTiles v3 ---------------------------------------------------------------------------------


def zxy_to_id(z, x, y):
    acc = (4 ** z - 1) // 3
    n, d, s = 1 << z, 0, (1 << z) >> 1
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        s >>= 1
    return acc + d


def id_to_zxy(tid):
    z = 0
    while (4 ** (z + 1) - 1) // 3 <= tid:
        z += 1
    d = tid - (4 ** z - 1) // 3
    x = y = 0
    s = 1
    while s < (1 << z):
        rx = 1 & (d // 2)
        ry = 1 & (d ^ rx)
        if ry == 0:
            if rx == 1:
                x, y = s - 1 - x, s - 1 - y
            x, y = y, x
        x += s * rx
        y += s * ry
        d //= 4
        s *= 2
    return z, x, y


# 127 bytes: the magic and version, four sections' offsets and lengths (root directory, metadata,
# leaf directories, tile data), three counts, six flags and zooms, the bounds, the center.
HEADER = struct.Struct('<7sB 8Q 3Q BBBB BB iiii B ii')
assert HEADER.size == 127


def decompress(b, kind):
    return gzip.decompress(b) if kind == 2 else bytes(b)


def parse_directory(b):
    n, i = varint(b, 0)
    ids, runs, lens, offs = [], [], [], []
    last = 0
    for _ in range(n):
        d, i = varint(b, i)
        last += d
        ids.append(last)
    for _ in range(n):
        r, i = varint(b, i)
        runs.append(r)
    for _ in range(n):
        ln, i = varint(b, i)
        lens.append(ln)
    for k in range(n):
        o, i = varint(b, i)
        offs.append(offs[k - 1] + lens[k - 1] if o == 0 and k > 0 else o - 1)
    return list(zip(ids, runs, lens, offs))


class Reader:
    def __init__(self, path):
        self.file = open(path, 'rb')
        self.data = mmap.mmap(self.file.fileno(), 0, access=mmap.ACCESS_READ)
        h = HEADER.unpack(self.data[:HEADER.size])
        if h[0] != b'PMTiles' or h[1] != 3:
            raise ValueError(f'{path}: not a PMTiles v3 archive')
        (_, _, self.root_offset, self.root_length, self.meta_offset, self.meta_length, self.leaf_offset,
         self.leaf_length, self.data_offset, self.data_length, _, _, _, _, self.internal, self.compression,
         self.tile_type, self.min_zoom, self.max_zoom, *self.bounds_e7, _, _, _) = h
        # A Protomaps leaf holds some 61,000 entries, 9 MB once read: a z14 tile's children come in
        # order, so a few leaves serve a worker (256 of them in 32 workers filled 125 GB, 16/10/2026).
        self.leaf = lru_cache(maxsize=4)(self._leaf)
        self.root = self.directory(self.root_offset, self.root_length)
        self.root_ids = [e[0] for e in self.root]

    def directory(self, offset, length):
        return parse_directory(decompress(self.data[offset:offset + length], self.internal))

    def _leaf(self, offset, length):
        entries = self.directory(self.leaf_offset + offset, length)
        return entries, [e[0] for e in entries]

    def metadata(self):
        return json.loads(decompress(self.data[self.meta_offset:self.meta_offset + self.meta_length], self.internal) or b'{}')

    def entries(self, offset=None, length=None):
        """Every tile entry, in order (a leaf at a time)."""
        if offset is None:
            directory = self.directory(self.root_offset, self.root_length)
        else:
            directory = self._leaf(offset, length)[0]
        for tid, run, ln, off in directory:
            if run == 0:
                yield from self.entries(off, ln)
            else:
                yield tid, run, ln, off

    def find(self, tid):
        entries, ids = self.root, self.root_ids
        for _ in range(4):
            lo = bisect.bisect_right(ids, tid)
            if lo == 0:
                return None
            e = entries[lo - 1]
            if e[1] == 0:
                entries, ids = self.leaf(e[3], e[2])
                continue
            return self.tile_at(e[3], e[2]) if tid < e[0] + e[1] else None
        return None

    def tile_at(self, offset, length):
        raw = self.data[self.data_offset + offset:self.data_offset + offset + length]
        return gzip.decompress(raw) if self.compression == 2 else bytes(raw)


def serialize_directory(ids, runs, lens, offs, start=0, end=None):
    end = len(ids) if end is None else end
    out = bytearray()
    put_varint(out, end - start)
    last = 0
    for k in range(start, end):
        put_varint(out, ids[k] - last)
        last = ids[k]
    for k in range(start, end):
        put_varint(out, runs[k])
    for k in range(start, end):
        put_varint(out, lens[k])
    for k in range(start, end):
        put_varint(out, 0 if k > start and offs[k] == offs[k - 1] + lens[k - 1] else offs[k] + 1)
    return gzip.compress(bytes(out), mtime=0)


def packed_tile(tid, run, tile):
    """A tile ready for the writer, gzipped in the worker: its key when small enough to be shared."""
    gz = gzip.compress(tile, mtime=0)
    key = int.from_bytes(hashlib.blake2b(gz, digest_size=8).digest(), 'little') if len(gz) < SHARED_BELOW else -1
    return tid, run, gz, key


class Writer:
    """Tiles in tile id order; runs of the same tile merged, small tiles stored once. The entries in
    four arrays of 64-bit integers: a world's, a few hundred million tuples, would not fit."""

    def __init__(self, path):
        self.path = path
        self.data = tempfile.NamedTemporaryFile(dir=os.path.dirname(os.path.abspath(path)), delete=False)
        self.offset = 0
        self.ids, self.runs, self.lens, self.offs = array('Q'), array('Q'), array('Q'), array('Q')
        self.shared = {}
        self.addressed = 0
        self.contents = 0
        self.zooms = {}
        self.zoom = None

    def add(self, tid, run, tile):
        self.add_packed(*packed_tile(tid, run, tile))

    def add_packed(self, tid, run, gz, key):
        self.addressed += run
        z = id_to_zxy(tid)[0]
        self.zoom = z
        count, size = self.zooms.get(z, (0, 0))
        self.zooms[z] = (count + run, size + len(gz))
        offset = self.shared.get(key) if key >= 0 else None
        if offset is not None:
            if self.ids and self.ids[-1] + self.runs[-1] == tid and self.offs[-1] == offset and self.lens[-1] == len(gz):
                self.runs[-1] += run
            else:
                self._entry(tid, run, len(gz), offset)
            return
        self.data.write(gz)
        if key >= 0:
            self.shared[key] = self.offset
        self._entry(tid, run, len(gz), self.offset)
        self.offset += len(gz)
        self.contents += 1

    def _entry(self, tid, run, length, offset):
        self.ids.append(tid)
        self.runs.append(run)
        self.lens.append(length)
        self.offs.append(offset)

    @property
    def entries(self):
        return len(self.ids)

    def finish(self, metadata, min_zoom, max_zoom, bounds_e7):
        self.data.close()
        meta = gzip.compress(json.dumps(metadata, separators=(',', ':')).encode(), mtime=0)
        leaf_size, leaves = 4096, b''
        count = len(self.ids)
        root = serialize_directory(self.ids, self.runs, self.lens, self.offs) if count <= 16384 else b'x' * 16384
        while HEADER.size + len(root) > 16384:
            leaves, roots = bytearray(), ([], [], [], [])
            for k in range(0, count, leaf_size):
                body = serialize_directory(self.ids, self.runs, self.lens, self.offs, k, min(k + leaf_size, count))
                for column, value in zip(roots, (self.ids[k], 0, len(body), len(leaves))):
                    column.append(value)
                leaves += body
            root = serialize_directory(*roots)
            leaf_size *= 2
        root_offset = HEADER.size
        meta_offset = root_offset + len(root)
        leaf_offset = meta_offset + len(meta)
        data_offset = leaf_offset + len(leaves)
        lon0, lat0, lon1, lat1 = bounds_e7
        header = HEADER.pack(b'PMTiles', 3, root_offset, len(root), meta_offset, len(meta), leaf_offset, len(leaves),
                             data_offset, self.offset, self.addressed, count, self.contents,
                             1, 2, 2, 1, min_zoom, max_zoom, lon0, lat0, lon1, lat1, min_zoom,
                             (lon0 + lon1) // 2, (lat0 + lat1) // 2)
        with open(self.path, 'wb') as out:
            out.write(header + root + meta + leaves)
            with open(self.data.name, 'rb') as tiles:
                while chunk := tiles.read(1 << 24):
                    out.write(chunk)
        os.unlink(self.data.name)


# --- The run ------------------------------------------------------------------------------------

READER = None
FAMILY_KINDS = frozenset()


def start_worker(source, family_kinds):
    global READER, FAMILY_KINDS
    READER = Reader(source)
    FAMILY_KINDS = frozenset(family_kinds)


def detail_chunk(chunk):
    out = []
    for tid, run, length, offset in chunk:
        z, x, y = id_to_zxy(tid)
        layers = decode(READER.tile_at(offset, length))
        result = detail_layers(layers, FAMILY_KINDS)
        # A run is the same tile over and over (the sea): nothing of its own to take from below.
        if z == 14 and run == 1:
            pois = next((layer for layer in result if layer[0] == 'pois'), None)
            extent = pois[1] if pois else layers.get('roads', (4096,))[0]
            present = {(f.props.get('kind'), f.props.get('name')) for f in (pois[2] if pois else [])}
            children = []
            for dy in (0, 1):
                for dx in (0, 1):
                    child = READER.find(zxy_to_id(15, 2 * x + dx, 2 * y + dy))
                    if child:
                        children.append((dx, dy, child))
            extra = lifted_pois(children, extent, FAMILY_KINDS, present)
            if extra:
                if pois:
                    pois[2].extend(extra)
                else:
                    result.append(('pois', extent, extra))
        tile = encode(result)
        if tile:
            out.append(packed_tile(tid, run, tile))
    return out


def buildings_chunk(chunk):
    out = []
    for tid, run, length, offset in chunk:
        tile = encode(building_layers(decode(READER.tile_at(offset, length))))
        if tile:
            out.append(packed_tile(tid, run, tile))
    return out


def chunks(reader, zooms, size=512):
    lo, hi = zxy_to_id(min(zooms), 0, 0), zxy_to_id(max(zooms) + 1, 0, 0)
    chunk = []
    for tid, run, length, offset in reader.entries():
        if tid + run <= lo:
            continue
        if tid >= hi:
            break
        # A run of the sea may cross a zoom's edge: only its part within the zooms is ours (the
        # world's first bake had three z8 tiles, and `pmtiles verify` refused it).
        start, end = max(tid, lo), min(tid + run, hi)
        chunk.append((start, end - start, length, offset))
        if len(chunk) == size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def run(kind, source, out, family_kinds, workers, build):
    reader = Reader(source)
    zooms = DETAIL_ZOOMS if kind == 'detail' else range(BUILDINGS_ZOOM, BUILDINGS_ZOOM + 1)
    writer = Writer(out)
    work = detail_chunk if kind == 'detail' else buildings_chunk
    started, done = time.time(), 0

    def take(results):
        nonlocal done
        for result in results:
            for packed_one in result:
                writer.add_packed(*packed_one)
            done += 1
            if done % 200 == 0:
                print(f'{time.strftime("%H:%M:%S")} z{writer.zoom} {writer.addressed} tiles, {writer.offset / 1e9:.2f} GB '
                      f'({time.time() - started:.0f} s)', flush=True)

    def bounded(pool):
        """The chunks' results in order, a few per worker in flight: `imap` would queue every result
        the writer has not taken yet, and a world's filled 125 GB (16/10/2026)."""
        pending = collections.deque()
        for chunk in chunks(reader, zooms):
            pending.append(pool.apply_async(work, (chunk,)))
            if len(pending) >= 4 * workers:
                yield pending.popleft().get()
        while pending:
            yield pending.popleft().get()

    if workers <= 1:
        # In this process: a small extract, or the tests (which load this file under another name).
        start_worker(source, family_kinds)
        take(map(work, chunks(reader, zooms)))
    else:
        with multiprocessing.Pool(workers, start_worker, (source, family_kinds)) as pool:
            take(bounded(pool))
    source_meta = reader.metadata()
    layers = DETAIL_LAYERS if kind == 'detail' else ('buildings',)
    metadata = {
        'name': os.path.splitext(os.path.basename(out))[0],
        'description': (
            'OpenStreetMap detail for the Foxar players, z9 to z14: roads, water, land uses, useful places, '
            'towns and neighbourhoods (foxar-geo SPEC.md section 32)' if kind == 'detail'
            else 'OpenStreetMap buildings for the Foxar players, z15 (foxar-geo SPEC.md section 32)'),
        'attribution': '© OpenStreetMap contributors (ODbL)',
        'license': 'ODbL-1.0',
        'source': f'Protomaps basemap {build or ""}, schema {source_meta.get("version", "?")}'.replace('  ', ' '),
        'vector_layers': [{'id': name, 'fields': {}} for name in layers],
    }
    writer.finish(metadata, min(zooms), max(zooms), reader.bounds_e7)
    print(json.dumps({'tiles': writer.addressed, 'entries': writer.entries, 'contents': writer.contents,
                      'bytes': os.path.getsize(out), 'seconds': round(time.time() - started),
                      'zooms': {z: {'tiles': c, 'bytes': s} for z, (c, s) in sorted(writer.zooms.items())}}))


def stats(path):
    reader = Reader(path)
    per = {}
    for tid, run, length, _ in reader.entries():
        z = id_to_zxy(tid)[0]
        tiles, size, biggest = per.get(z, (0, 0, 0))
        per[z] = (tiles + run, size + length * run, max(biggest, length))
    for z, (tiles, size, biggest) in sorted(per.items()):
        print(f'z{z}: {tiles} tiles, {size / 1e6:.1f} MB addressed, biggest {biggest / 1e3:.0f} kB')


def family_kinds_of(style_path):
    with open(style_path) as file:
        style = json.load(file)
    families = style.get('poiFamilies') or style.get('pois') or {}
    kinds = set()
    for family in families.values():
        kinds.update(family['kinds'] if isinstance(family, dict) else family)
    if not kinds:
        raise SystemExit(f'{style_path}: no useful places\' kinds')
    return sorted(kinds)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('kind', choices=('detail', 'buildings', 'stats'))
    parser.add_argument('paths', nargs='+')
    parser.add_argument('--workers', type=int, default=os.cpu_count())
    parser.add_argument('--build', default='')
    args = parser.parse_args()
    if args.kind == 'stats':
        stats(args.paths[0])
        return
    if os.path.exists(args.paths[1]):
        sys.exit(f'{args.paths[1]} exists: a name never changes content, remove it by hand if it was never published')
    kinds = family_kinds_of(args.paths[2]) if args.kind == 'detail' else []
    run(args.kind, args.paths[0], args.paths[1], kinds, args.workers, args.build)


if __name__ == '__main__':
    main()
