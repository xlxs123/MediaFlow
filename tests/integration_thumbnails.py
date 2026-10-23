"""真实 FFmpeg 的纯本地缩略图验收；不访问网站或下载第三方媒体。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.output_validator import validate_output
from utils.thumbnails import generate_thumbnail


def command(args):
    return subprocess.run(args, shell=False, stdin=subprocess.DEVNULL, capture_output=True,
                          encoding='utf-8', check=True, timeout=40,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def jpeg_probe(path):
    data = json.loads(command([str(ROOT / 'tools' / 'ffprobe.exe'), '-v', 'error', '-show_entries',
                               'format=format_name:stream=codec_name,codec_type,width,height',
                               '-of', 'json', str(path)]).stdout)
    stream = next(item for item in data['streams'] if item['codec_type'] == 'video')
    assert stream['codec_name'] == 'mjpeg' and 0 < stream['width'] <= 480 and 0 < stream['height'] <= 270, data
    return data


def main():
    directory = (Path(sys.argv[1]) if len(sys.argv) > 1 else
                 ROOT / 'verification' / 'brand-thumbnail-local' / str(time.time_ns())).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    media = directory / 'media'
    media.mkdir(exist_ok=True)
    ffmpeg = ROOT / 'tools' / 'ffmpeg.exe'
    checks = []
    try:
        for name, duration in (('local-video.mp4', 4.0), ('short-fallback.mp4', 0.4)):
            video = media / name
            command([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-n',
                     '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=25',
                     '-f', 'lavfi', '-i', 'sine=frequency=640:sample_rate=44100',
                     '-t', str(duration), '-c:v', 'libx264', '-preset', 'ultrafast',
                     '-pix_fmt', 'yuv420p', '-threads', '1', '-c:a', 'aac', '-movflags', '+faststart', str(video)])
            validation = validate_output(video, ffmpeg_path=ffmpeg, expected_extension='mp4')
            original = digest(video)
            result = generate_thumbnail(video, ffmpeg_path=ffmpeg, validation=validation)
            assert result['status'] == 'generated', result
            cover = Path(result['path'])
            assert cover == video.with_suffix('.jpg') and cover.is_file(), result
            assert digest(video) == original
            info = jpeg_probe(cover)
            expected = [1.0] if duration > 1 else [1.0, 3.0, round(validation['duration'] / 2, 6)]
            assert result['attempts'] == expected, (result, validation)
            checks.append({'name': 'real_mp4_first_seek' if duration > 1 else 'real_short_mp4_midpoint_fallback',
                           'passed': True, 'source': 'FFmpeg 自生成本地 MP4', 'result': result,
                           'jpeg': info, 'video_unchanged': True})
            if duration > 1:
                original_cover = digest(cover)
                second = generate_thumbnail(video, ffmpeg_path=ffmpeg, validation=validation)
                assert second['status'] == 'existing' and second['attempts'] == [], second
                assert digest(cover) == original_cover and digest(video) == original
                checks.append({'name': 'existing_real_jpeg_preserved', 'passed': True, 'result': second,
                               'cover_unchanged': True, 'video_unchanged': True})

        audio = media / 'local-audio.mp3'
        command([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-n', '-f', 'lavfi',
                 '-i', 'sine=frequency=440:sample_rate=44100', '-t', '1.2',
                 '-c:a', 'libmp3lame', '-q:a', '4', str(audio)])
        validation = validate_output(audio, ffmpeg_path=ffmpeg, expected_extension='mp3')
        original = digest(audio)
        result = generate_thumbnail(audio, ffmpeg_path=ffmpeg, validation=validation)
        assert result['status'] == 'skipped' and result['attempts'] == [], result
        assert not audio.with_suffix('.jpg').exists() and digest(audio) == original
        checks.append({'name': 'real_mp3_skipped', 'passed': True, 'source': 'FFmpeg 自生成本地 MP3',
                       'result': result, 'audio_unchanged': True})

        external_cover_video = media / 'unknown-cover.mp4'
        shutil.copyfile(media / 'local-video.mp4', external_cover_video)
        cover = external_cover_video.with_suffix('.jpg')
        cover.write_bytes(b'Existing user cover; must not overwrite')
        original = digest(external_cover_video)
        original_cover = digest(cover)
        validation = validate_output(external_cover_video, ffmpeg_path=ffmpeg, expected_extension='mp4')
        result = generate_thumbnail(external_cover_video, ffmpeg_path=ffmpeg, validation=validation)
        assert result['status'] == 'failed' and result['attempts'] == [], result
        assert digest(cover) == original_cover and digest(external_cover_video) == original
        checks.append({'name': 'unknown_existing_cover_preserved', 'passed': True,
                       'result': result, 'cover_unchanged': True, 'video_unchanged': True})
        assert not list(media.glob('.mediaflow-thumbnail-*'))
        report = {'passed': True, 'checks': checks, 'online_site_test': False,
                  'fixture': '纯本地 FFmpeg MP4/MP3；不是 Rou/Douyin 或其他真实站点测试',
                  'ffmpeg_sha256': digest(ffmpeg)}
    except Exception:
        report = {'passed': False, 'checks': checks, 'online_site_test': False,
                  'traceback': traceback.format_exc()}
    (directory / 'thumbnail-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
