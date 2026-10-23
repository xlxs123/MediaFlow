from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier
import unittest

from core.file_names import FileNameAllocator, sanitize_filename


class FileNameTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.allocator = FileNameAllocator()

    def test_scenario03_three_equal_titles_are_numbered_immediately(self):
        paths = [self.allocator.allocate(self.directory, 'Video.mp4') for _ in range(3)]
        self.assertEqual([path.name for path in paths], ['Video.mp4', 'Video (1).mp4', 'Video (2).mp4'])
        self.assertFalse(any(path.exists() for path in paths))

    def test_scenario04_existing_disk_files_continue_at_two(self):
        for name in ('Video.mp4', 'Video (1).mp4'):
            (self.directory / name).write_bytes(b'existing media')
        self.assertEqual(self.allocator.allocate(self.directory, 'Video.mp4').name, 'Video (2).mp4')
        self.assertEqual((self.directory / 'Video.mp4').read_bytes(), b'existing media')

    def test_scenario05_twenty_concurrent_same_titles_have_no_conflicts(self):
        start = Barrier(20)

        def allocate(_):
            start.wait(timeout=10)
            return self.allocator.allocate(self.directory, 'Video.mp4')

        with ThreadPoolExecutor(max_workers=20) as pool:
            paths = list(pool.map(allocate, range(20)))
        self.assertEqual(len({str(path).casefold() for path in paths}), 20)
        self.assertEqual({path.name for path in paths}, {'Video.mp4'} | {f'Video ({number}).mp4' for number in range(1, 20)})

    def test_scenario10_all_quality_names_number_before_extension(self):
        first = self.allocator.allocate(self.directory, 'Title [1080p].mp4')
        second = self.allocator.allocate(self.directory, 'Title [720p].mp4')
        repeated = self.allocator.allocate(self.directory, 'Title [1080p].mp4')
        self.assertEqual(first.name, 'Title [1080p].mp4')
        self.assertEqual(second.name, 'Title [720p].mp4')
        self.assertEqual(repeated.name, 'Title [1080p] (1).mp4')

    def test_illegal_characters_and_control_characters_are_cleaned(self):
        name = sanitize_filename('  A\\B/C:D*E?F"G<H>I|J\x00\n.mp4  ')
        self.assertEqual(name, 'A_B_C_D_E_F_G_H_I_J__.mp4')
        self.assertFalse(any(character in name for character in '\\/:*?"<>|\x00\n'))

    def test_leading_trailing_whitespace_and_terminal_dots_are_removed(self):
        self.assertEqual(sanitize_filename('  Video.mp4...  '), 'Video.mp4')
        self.assertEqual(sanitize_filename(' Video .mp4 '), 'Video.mp4')

    def test_windows_device_names_are_safely_prefixed(self):
        for name in ('CON', 'PRN', 'AUX', 'NUL', 'COM1', 'com9', 'LPT1', 'lpt9', 'COM¹', 'LPT²', 'CONIN$', 'CONOUT$'):
            with self.subTest(name=name):
                self.assertEqual(sanitize_filename(name + '.mp4'), '_' + name + '.mp4')

    def test_reserved_device_name_with_multiple_extensions_is_safe(self):
        self.assertEqual(sanitize_filename('CON.metadata.mp4'), '_CON.metadata.mp4')
        self.assertEqual(sanitize_filename('NUL .mp4'), '_NUL.mp4')

    def test_non_device_prefixes_do_not_get_unnecessary_renaming(self):
        for name in ('CONcert.mp4', 'COM10.mp4', 'LPT0.mp4', 'NULl.mp4'):
            with self.subTest(name=name):
                self.assertEqual(sanitize_filename(name), name)

    def test_truncation_cannot_create_a_windows_device_name(self):
        self.assertEqual(sanitize_filename('CONcert.mp4', max_length=7), '_CO.mp4')

    def test_empty_filename_has_safe_default(self):
        for name in ('', '  ', '.', '..', '...'):
            with self.subTest(name=name):
                self.assertEqual(sanitize_filename(name), 'media')
        self.assertEqual(sanitize_filename('.mp4'), 'media.mp4')

    def test_output_container_extensions_are_preserved(self):
        for extension in ('mp4', 'webm', 'mkv', 'm4a', 'mp3', 'opus', 'mov', 'aac'):
            with self.subTest(extension=extension):
                desired = f'Video.{extension}'
                self.assertEqual(self.allocator.allocate(self.directory, desired).name, desired)
                self.assertEqual(self.allocator.allocate(self.directory, desired).name, f'Video (1).{extension}')

    def test_windows_length_preserves_container_extension(self):
        name = sanitize_filename('视' * 1000 + '.mp4')
        self.assertEqual(len(name), 180)
        self.assertTrue(name.endswith('.mp4'))

    def test_astral_unicode_counts_as_two_windows_units_without_splitting(self):
        name = sanitize_filename('🎬' * 1000 + '.mkv')
        self.assertEqual(len(name.encode('utf-16-le')) // 2, 180)
        self.assertTrue(name.endswith('.mkv'))

    def test_custom_length_limit_and_windows_hard_limit(self):
        self.assertEqual(sanitize_filename('Video.mp4', max_length=7), 'Vid.mp4')
        self.assertEqual(len(sanitize_filename('x' * 1000 + '.mp4', max_length=1000)), 255)

    def test_invalid_or_insufficient_length_limit_raises(self):
        for limit in (0, -1, 2.5, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                sanitize_filename('Video.mp4', max_length=limit)
        with self.assertRaises(ValueError):
            sanitize_filename('Video.mp4', max_length=4)

    def test_long_collision_names_remain_bounded(self):
        paths = [self.allocator.allocate(self.directory, '视' * 1000 + '.mp4') for _ in range(12)]
        self.assertEqual(len({path.name for path in paths}), 12)
        self.assertTrue(all(len(path.name.encode('utf-16-le')) // 2 <= 180 for path in paths))
        self.assertTrue(paths[-1].name.endswith(' (11).mp4'))

    def test_long_unicode_titles_keep_quality_label_with_disk_and_batch_collisions(self):
        title = '视😀' * 200 + ' [1080p].mp4'
        initial = sanitize_filename(title)
        self.assertTrue(initial.endswith(' [1080p].mp4'))
        (self.directory / initial).write_bytes(b'keep')
        paths = [self.allocator.allocate(self.directory, title) for _ in range(20)]
        self.assertEqual(len({path.name.casefold() for path in paths}), 20)
        self.assertTrue(all(' [1080p] (' in path.name for path in paths))
        self.assertTrue(all(len(path.name.encode('utf-16-le')) // 2 <= 180 for path in paths))
        self.assertTrue(paths[-1].name.endswith(' [1080p] (20).mp4'))
        self.assertEqual((self.directory / initial).read_bytes(), b'keep')

    def test_disk_collision_is_case_insensitive(self):
        (self.directory / 'VIDEO.MP4').write_bytes(b'old')
        self.assertEqual(self.allocator.allocate(self.directory, 'video.mp4').name, 'video (1).mp4')

    def test_current_batch_reservation_is_case_insensitive(self):
        self.allocator.allocate(self.directory, 'Video.mp4')
        self.assertEqual(self.allocator.allocate(self.directory, 'VIDEO.MP4').name, 'VIDEO (1).MP4')

    def test_directories_also_occupy_names(self):
        (self.directory / 'Video.mp4').mkdir()
        self.assertEqual(self.allocator.allocate(self.directory, 'Video.mp4').name, 'Video (1).mp4')

    def test_disk_partials_and_ytdl_checkpoints_occupy_output_name(self):
        (self.directory / 'Video.mp4.PART').write_bytes(b'partial')
        (self.directory / 'Video (1).mp4.ytdl').write_text('{}', encoding='utf-8')
        (self.directory / 'Video (2).mp4.partial').write_bytes(b'partial')
        (self.directory / 'Video (3).mp4.part-Frag1').write_bytes(b'fragment')
        self.assertEqual(self.allocator.allocate(self.directory, 'Video.mp4').name, 'Video (4).mp4')

    def test_explicit_reserved_names_are_checked(self):
        path = self.allocator.allocate(self.directory, 'Video.mp4', reserved_names=('VIDEO.MP4', 'Video (1).mp4'))
        self.assertEqual(path.name, 'Video (2).mp4')

    def test_explicit_reserved_paths_only_apply_to_the_same_directory(self):
        other = self.directory / 'other'
        other.mkdir()
        path = self.allocator.allocate(self.directory, 'Video.mp4', reserved_names=(other / 'Video.mp4',))
        self.assertEqual(path.name, 'Video.mp4')
        self.allocator.release(path)
        path = self.allocator.allocate(self.directory, 'Video.mp4', reserved_names=(self.directory / 'Video.mp4',))
        self.assertEqual(path.name, 'Video (1).mp4')

    def test_reserve_and_release_do_not_delete_or_create_files(self):
        output = self.directory / 'Video.mp4'
        self.assertEqual(self.allocator.reserve(output), output.resolve())
        self.assertEqual(self.allocator.allocate(self.directory, 'Video.mp4').name, 'Video (1).mp4')
        self.allocator.release(output)
        self.assertFalse(output.exists())
        self.assertEqual(self.allocator.allocate(self.directory, 'Video.mp4'), output.resolve())

    def test_release_does_not_make_existing_disk_file_available(self):
        output = self.directory / 'Video.mp4'
        output.write_bytes(b'existing')
        self.allocator.reserve(output)
        self.allocator.release(output)
        self.assertEqual(self.allocator.allocate(self.directory, 'Video.mp4').name, 'Video (1).mp4')

    def test_reserve_partial_also_occupies_final_output_name(self):
        self.allocator.reserve(self.directory / 'Video.mp4.part')
        self.assertEqual(self.allocator.allocate(self.directory, 'Video.mp4').name, 'Video (1).mp4')

    def test_two_directories_have_independent_number_sequences(self):
        other = self.directory / 'other'
        first = self.allocator.allocate(self.directory, 'Video.mp4')
        second = self.allocator.allocate(other, 'Video.mp4')
        self.assertEqual(first.name, second.name)
        self.assertNotEqual(first.parent, second.parent)
        self.assertFalse(other.exists())

    def test_path_input_cannot_escape_target_directory(self):
        for value in ('../outside.mp4', '..\\outside.mp4', 'C:\\outside\\Video.mp4', '/outside/Video.mp4'):
            with self.subTest(value=value):
                output = self.allocator.allocate(self.directory, value)
                self.assertEqual(output.parent, self.directory.resolve())
                self.assertFalse(any(character in output.name for character in '\\/:'))
                self.assertFalse(output.exists())

    def test_returns_absolute_path_for_relative_directory(self):
        output = self.allocator.allocate(Path('.'), Path('FileNameAllocatorRelativeFixture.mp4'))
        self.assertTrue(output.is_absolute())
        self.assertEqual(output.parent, Path.cwd().resolve())

    def test_target_directory_cannot_be_an_existing_file(self):
        target = self.directory / 'file'
        target.write_bytes(b'data')
        with self.assertRaises(NotADirectoryError):
            self.allocator.allocate(target, 'Video.mp4')


if __name__ == '__main__':
    unittest.main()
