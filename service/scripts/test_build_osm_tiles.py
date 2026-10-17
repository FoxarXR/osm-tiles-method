"""build-osm-tiles.py against a made-up Protomaps extract: python3 -m unittest discover -s service/scripts"""

import importlib.util
import os
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('bake', os.path.join(os.path.dirname(__file__), 'build-osm-tiles.py'))
bake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bake)

STYLE = os.path.join(os.path.dirname(__file__), '..', '..', 'style', 'osm-style.json')


def text(value):
    out = bytearray()
    bake.put_bytes(out, 1, value.encode())
    return bytes(out)


def boolean(value):
    out = bytearray()
    bake.put_uint(out, 7, 1 if value else 0)
    return bytes(out)


def sint(value):
    out = bytearray()
    bake.put_uint(out, 6, (value << 1) ^ (value >> 63))
    return bytes(out)


def feature(gtype, geometry, **props):
    raws = {k.replace('__', ':'): boolean(v) if isinstance(v, bool) else sint(v) if isinstance(v, int) else text(v)
            for k, v in props.items()}
    return bake.Feature(None, gtype, geometry, {k: bake.value_of(v) for k, v in raws.items()}, raws)


LINE = bytes([9, 0, 0, 10, 20, 20])  # MoveTo(0, 0) LineTo(10, 10)
SQUARE = bytes([9, 0, 0, 26, 20, 0, 0, 20, 19, 0, 15])  # a closed square


def point(x, y):
    return bake.point_geometry(x, y)


class BakeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        source = os.path.join(cls.folder.name, 'source.pmtiles')
        x, y = 8471, 5847  # Dijon at z14
        z14 = bake.encode([
            ('roads', 4096, [
                feature(2, LINE, kind='minor_road', kind_detail='residential', name='Rue Chabot-Charny', oneway='yes',
                        sort_rank=300, name__en='Chabot-Charny Street'),
                feature(2, LINE, kind='path', kind_detail='sidewalk'),
                feature(2, LINE, kind='major_road', kind_detail='primary', is_tunnel=True),
            ]),
            ('pois', 4096, [
                feature(1, point(100, 100), kind='school', name='Lycée Carnot', min_zoom=14),
                feature(1, point(200, 200), kind='school', min_zoom=14),
                feature(1, point(300, 300), kind='supermarket', name='Market', min_zoom=14),
            ]),
            ('places', 4096, [
                feature(1, point(50, 50), kind='neighbourhood', name='Centre-Ville', population_rank=9, sort_key=3),
                feature(1, point(60, 60), kind='country', name='France'),
            ]),
            ('water', 4096, [
                feature(2, LINE, kind='stream', layer=-1),
                feature(2, LINE, kind='river', name='Ouche'),
                feature(3, SQUARE, kind='water'),
            ]),
            ('buildings', 4096, [feature(3, SQUARE, kind='building')]),
            ('earth', 4096, [feature(3, SQUARE, kind='earth')]),
            ('boundaries', 4096, [feature(2, LINE, kind='locality')]),
        ])
        children = {
            # A school Protomaps keeps for z15, at the child's (1000, 2000): its parent's (3048, 3048).
            (1, 1): bake.encode([('pois', 4096, [
                feature(1, point(1000, 2000), kind='school', name='École Trémouille', min_zoom=15),
                feature(1, point(5000, 10), kind='museum', name='In the buffer', min_zoom=15),
                feature(1, point(10, 10), kind='tree', min_zoom=15),
            ])]),
            # The one its parent has already.
            (0, 0): bake.encode([
                ('pois', 4096, [feature(1, point(200, 200), kind='school', name='Lycée Carnot', min_zoom=14)]),
                ('buildings', 4096, [
                    feature(3, SQUARE, kind='building', height=12, addr_housenumber='3'),
                    feature(1, point(5, 5), kind='address', addr_housenumber='3'),
                ]),
            ]),
        }
        writer = bake.Writer(source)
        entries = [(bake.zxy_to_id(14, x, y), 1, z14)]
        entries += [(bake.zxy_to_id(15, 2 * x + dx, 2 * y + dy), 1, tile) for (dx, dy), tile in children.items()]
        # The sea: one tile, then the same three times over, at z14 beside.
        sea = bake.encode([('water', 4096, [feature(3, SQUARE, kind='ocean')])])
        entries += [(bake.zxy_to_id(14, x + 1, y), 1, sea), (bake.zxy_to_id(14, x + 1, y) + 1, 3, sea)]
        # A run of the sea from z8's last tile into z9's first two.
        entries.append((bake.zxy_to_id(9, 0, 0) - 1, 3, sea))
        for tid, run, tile in sorted(entries):
            writer.add(tid, run, tile)
        writer.finish({'version': '4.15.2'}, 8, 15, (50000000, 470000000, 51000000, 473000000))
        cls.detail = os.path.join(cls.folder.name, 'detail.pmtiles')
        cls.buildings = os.path.join(cls.folder.name, 'buildings.pmtiles')
        bake.run('detail', source, cls.detail, bake.family_kinds_of(STYLE), 1, '20261016')
        bake.run('buildings', source, cls.buildings, [], 1, '20261016')
        cls.x, cls.y = x, y

    @classmethod
    def tearDownClass(cls):
        cls.folder.cleanup()

    def layers(self, path, z, x, y):
        tile = bake.Reader(path).find(bake.zxy_to_id(z, x, y))
        self.assertIsNotNone(tile, f'{z}/{x}/{y}')
        return {name: [f.props for f in features] for name, (_, features) in bake.decode(tile).items()}

    def test_the_detail_keeps_the_layers_and_features_the_players_draw(self):
        layers = self.layers(self.detail, 14, self.x, self.y)
        self.assertEqual(sorted(layers), ['places', 'pois', 'roads', 'water'])
        self.assertEqual(layers['roads'], [{'kind': 'minor_road', 'kind_detail': 'residential', 'name': 'Rue Chabot-Charny',
                                            'name:en': 'Chabot-Charny Street'}])
        self.assertEqual([p['kind'] for p in layers['water']], ['river', 'water'])
        self.assertEqual(layers['places'], [{'kind': 'neighbourhood', 'name': 'Centre-Ville', 'population_rank': 9}])

    def test_a_z14_tile_takes_its_childrens_useful_places_once(self):
        tile = bake.Reader(self.detail).find(bake.zxy_to_id(14, self.x, self.y))
        _, pois = bake.decode(tile)['pois']
        self.assertEqual([(f.props['name'], f.props['min_zoom']) for f in pois], [('Lycée Carnot', 14), ('École Trémouille', 15)])
        self.assertEqual(bake.points(pois[1].geometry), [(2548, 3048)])

    def test_a_run_across_a_zooms_edge_keeps_its_part_within_the_zooms(self):
        entries = list(bake.Reader(self.detail).entries())
        first = bake.zxy_to_id(9, 0, 0)
        self.assertEqual(entries[0][:2], (first, 2))
        self.assertTrue(all(tid >= first for tid, *_ in entries))

    def test_the_sea_stays_one_tile_repeated(self):
        reader = bake.Reader(self.detail)
        entries = list(reader.entries())
        sea = [e for e in entries if e[0] == bake.zxy_to_id(14, self.x + 1, self.y)]
        self.assertEqual(len(sea), 1)
        self.assertEqual(sea[0][1], 4)
        self.assertEqual(self.layers(self.detail, 14, self.x + 1, self.y)['water'], [{'kind': 'ocean'}])

    def test_the_buildings_archive_holds_z15_polygons_alone(self):
        reader = bake.Reader(self.buildings)
        self.assertEqual((reader.min_zoom, reader.max_zoom), (15, 15))
        layers = self.layers(self.buildings, 15, 2 * self.x, 2 * self.y)
        self.assertEqual(layers, {'buildings': [{'kind': 'building', 'height': 12}]})
        self.assertIsNone(reader.find(bake.zxy_to_id(15, 2 * self.x + 1, 2 * self.y + 1)))

    def test_the_metadata_says_the_licence(self):
        meta = bake.Reader(self.detail).metadata()
        self.assertEqual(meta['license'], 'ODbL-1.0')
        self.assertIn('© OpenStreetMap contributors', meta['attribution'])
        self.assertEqual([layer['id'] for layer in meta['vector_layers']], ['roads', 'water', 'landuse', 'pois', 'places'])

    def test_an_archive_past_a_root_directory_reads_through_its_leaves(self):
        path = os.path.join(self.folder.name, 'many.pmtiles')
        writer = bake.Writer(path)
        first = bake.zxy_to_id(14, 0, 0)
        for k in range(40000):
            writer.add(first + 2 * k, 1, f'tile {k}'.encode() * 200)
        writer.finish({}, 14, 14, (0, 0, 10, 10))
        reader = bake.Reader(path)
        self.assertGreater(reader.leaf_length, 0)
        for k in (0, 1, 19999, 39999):
            self.assertEqual(reader.find(first + 2 * k), f'tile {k}'.encode() * 200)
        self.assertIsNone(reader.find(first + 1))
        self.assertEqual(sum(1 for _ in reader.entries()), 40000)

    def test_tile_ids_go_both_ways(self):
        for z, x, y in [(0, 0, 0), (9, 264, 182), (14, 8471, 5847), (15, 16943, 11695)]:
            self.assertEqual(bake.id_to_zxy(bake.zxy_to_id(z, x, y)), (z, x, y))


if __name__ == '__main__':
    unittest.main()
