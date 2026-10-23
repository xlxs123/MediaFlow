from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from core.output_organization import (check_collection, classification_parts, create_collection,
    directory_for, filename_limit, make_values, normalize_options, render_filename)
from core.file_names import _units


class OutputOrganizationTests(unittest.TestCase):
    def setUp(self):
        self.values = make_values('中文标题<>:"/\\|?*', source_url='https://www.bilibili.com/video/BV123',
            resolution=(1920, 1080), timestamp=datetime(2026, 10, 21, 9, 2, 3))

    def test_chinese_and_illegal_characters(self):
        name = render_filename('{site}_{title}_{resolution}_{date}.{ext}', self.values, 'mp4')
        self.assertTrue(name.startswith('Bilibili_中文标题'))
        self.assertTrue(name.endswith('_1080p_20261021.mp4'))
        self.assertFalse(set('<>:"/\\|?*') & set(name))
        self.assertEqual(render_filename('{title}.{ext}',dict(self.values,title='中文 / 标题 <> 内容'),'mp4'),
                         '中文_标题_内容.mp4')

    def test_empty_and_unknown(self):
        values = make_values(timestamp=datetime(2026, 10, 21))
        self.assertEqual(render_filename('{title}_{unknown}.{ext}', values, 'mp3'), 'media_20261021_000000_未知.mp3')
        self.assertEqual(values['site'], '未知网站')

    def test_reserved_and_trailing(self):
        self.assertEqual(render_filename('CON. ', self.values, 'mp4'), '_CON.mp4')
        self.assertEqual(render_filename('NUL.foo', self.values, 'mp4'), '_NUL.foo.mp4')

    def test_true_extension(self):
        self.assertTrue(render_filename('{title}.mkv', self.values, 'mp4').endswith('.mp4'))
        self.assertTrue(render_filename('{title}.{ext}', self.values, 'mkv').endswith('.mkv'))

    def test_literal_percent_and_braces(self):
        self.assertEqual(render_filename('100% {{title}}.{ext}', self.values, 'mp4'), '100% {title}.mp4')
        for template in ('{title.__class__}', '{title[0]}', '{title!r}', '{title:>20}', '{'):
            with self.assertRaises(ValueError):
                render_filename(template, self.values, 'mp4')

    def test_length_preserves_extension_and_chinese(self):
        name = render_filename('{title}.{ext}', dict(self.values, title='中文😀' * 200), 'mp4')
        self.assertLessEqual(_units(name), 180)
        self.assertIn('中文', name)
        self.assertTrue(name.endswith('.mp4'))

    def test_path_budget(self):
        directory = Path('C:/') / ('x' * 150)
        name = render_filename('{title}', self.values, 'mp4', directory)
        self.assertLessEqual(_units(str(directory.resolve() / name)) + 32, 240)
        with self.assertRaises(ValueError):
            filename_limit(Path('C:/') / ('a' * 230))

    def test_classification(self):
        self.assertEqual(classification_parts({'classification':'type_site'}, self.values), ['视频','Bilibili'])
        self.assertEqual(classification_parts({'classification':'date'}, self.values), ['20261021'])
        self.assertEqual(classification_parts({'classification':'type'}, make_values(extension='mp3')), ['音频'])
        self.assertEqual(classification_parts({'classification':'type'}, make_values(extension='srt')), ['字幕'])

    def test_custom_directory_and_containment(self):
        with tempfile.TemporaryDirectory() as root:
            options = {'classification':'custom','directory_template':'{site}/{date}'}
            self.assertEqual(directory_for(root, options, self.values), Path(root).resolve()/'Bilibili'/'20261021')
            for template in ('../outside', 'a/../../outside', 'C:/outside', '/outside', '.stream-downloads'):
                with self.assertRaises(ValueError):
                    directory_for(root, dict(options, directory_template=template), self.values)
            safe = directory_for(root, options, dict(self.values, site='../../outside'))
            self.assertTrue(safe.is_relative_to(Path(root).resolve()))
            with self.assertRaises(ValueError):
                directory_for(root, {}, {}, str(Path(root).parent))

    def test_collection_exclusive_and_identity(self):
        with tempfile.TemporaryDirectory() as root:
            old = Path(root)/'我的合集';old.mkdir();(old/'keep.txt').write_text('keep')
            first = create_collection(root, '我的合集', 'one')
            second = create_collection(root, '我的合集', 'two')
            self.assertNotEqual(first, second)
            self.assertNotEqual(first, str(old))
            self.assertEqual(check_collection(root, first, 'one'), Path(first))
            with self.assertRaises(ValueError):
                check_collection(root, first, 'two')
            self.assertEqual((old/'keep.txt').read_text(), 'keep')

    def test_defaults_and_actual_media_id(self):
        self.assertEqual(normalize_options(), {'filename_template':'','classification':'none','directory_template':''})
        values = make_values(source_url='https://www.douyin.com/user/self?modal_id=7526879737634950451')
        self.assertEqual(values['id'], '7526879737634950451')
        self.assertEqual(values['site'], 'Douyin')
        self.assertEqual(make_values(media_id='https://secret.test/a?token=abc')['id'], '未知ID')
