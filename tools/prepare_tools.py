"""从官方链接准备可携带的外部工具，校验发布方提供的 SHA256。"""
from pathlib import Path
import argparse
import hashlib
import json
import re
import shutil
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
YT_BASE = 'https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/'
YT_SHA = '66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a'
FFMPEG_NAME = 'ffmpeg-n9.0.2-33-g4cf298e66a-win64-gpl-9.0.zip'
FFMPEG_URL = 'https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-22-13-06/' + FFMPEG_NAME
FFMPEG_SHA = '5a8898e31f2c452062d1f1be475befd8ffba04db11e9f8251da3d10d9a1ae2e5'
FFMPEG_FILES = {'ffmpeg.exe':'1cbff81436de6364012b356726b46edfde4200f9d38d4622ff4f4978af0218f8',
                'ffprobe.exe':'3298debc7334bc482b771541ada41e5f6208f6e4014fc644ed7ebd0ac5cd9709'}


def download(url: str, destination: Path):
    request = urllib.request.Request(url, headers={'User-Agent': 'M3U8Downloader-Build/1.0'})
    with urllib.request.urlopen(request, timeout=45) as response, destination.open('wb') as handle:
        shutil.copyfileobj(response, handle)


def verify(path, expected):
    with path.open('rb') as handle:
        hasher = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            hasher.update(chunk)
        digest = hasher.hexdigest()
    if digest.lower() != expected.lower():
        raise RuntimeError(f'{path.name} SHA256 校验失败，未安装。')
    return digest


def main(archive_path=None, replace_cache=False):
    ROOT.mkdir(parents=True, exist_ok=True)
    provenance = {}
    yt = ROOT / 'yt-dlp.exe'
    if not yt.exists():
        base = YT_BASE
        sums = ROOT / 'yt-dlp-SHA2-256SUMS.txt'
        download(base + 'SHA2-256SUMS', sums)
        match = re.search(r'^([a-fA-F0-9]{64})\s+\*?yt-dlp\.exe\s*$', sums.read_text(), re.M)
        if not match:
            raise RuntimeError('发布方校验清单中未找到 yt-dlp.exe。')
        temporary = yt.with_suffix('.download')
        download(base + 'yt-dlp.exe', temporary)
        if match.group(1).lower() != YT_SHA:
            raise RuntimeError('yt-dlp 固定版本的上游校验清单发生变化，停止构建。')
        provenance['yt-dlp.exe'] = {'source': base + 'yt-dlp.exe', 'sha256': verify(temporary, YT_SHA)}
        temporary.replace(yt)
        print('yt-dlp.exe 已准备，SHA256 验证通过。', flush=True)
    provenance['yt-dlp.exe'] = {'source':YT_BASE+'yt-dlp.exe', 'sha256':verify(yt,YT_SHA)}
    existing = [ROOT/name for name in FFMPEG_FILES if (ROOT/name).exists()]
    if not replace_cache:
        for path in existing:
            verify(path,FFMPEG_FILES[path.name])
    if replace_cache or len(existing) != len(FFMPEG_FILES):
        archive = Path(archive_path) if archive_path else ROOT / FFMPEG_NAME
        if not archive.exists():
            download(FFMPEG_URL,archive)
        digest = verify(archive, FFMPEG_SHA)
        with zipfile.ZipFile(archive) as bundle:
            for filename in ('ffmpeg.exe', 'ffprobe.exe'):
                member = next(info for info in bundle.infolist() if info.filename.endswith('/bin/' + filename))
                with bundle.open(member) as source, (ROOT / filename).open('wb') as target:
                    shutil.copyfileobj(source, target)
                provenance[filename] = {'source': FFMPEG_URL, 'archive_sha256': digest}
                with (ROOT / filename).open('rb') as handle:
                    hasher = hashlib.sha256()
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        hasher.update(chunk)
                    provenance[filename]['sha256'] = hasher.hexdigest()
                verify(ROOT/filename,FFMPEG_FILES[filename])
            notice = next(info for info in bundle.infolist() if info.filename.endswith('/LICENSE.txt'))
            (ROOT/'FFmpeg-LICENSE').write_bytes(bundle.read(notice))
        (ROOT/'ffmpeg-SHA256.txt').write_text(FFMPEG_SHA+'  '+FFMPEG_NAME+'\n',encoding='utf-8')
        print('ffmpeg.exe / ffprobe.exe 已准备，发行包 SHA256 验证通过。', flush=True)
    for filename,digest in FFMPEG_FILES.items():
        provenance[filename]={'source':FFMPEG_URL,'archive_sha256':FFMPEG_SHA,
            'sha256':verify(ROOT/filename,digest),'build_recipe_commit':'937868360a21d711ec94a33f5591d345ad9b7754',
            'ffmpeg_commit':'4cf298e66a','upstream_workflow_run':39776938430}
    for filename, source in (
        ('yt-dlp-LICENSE.txt', 'https://raw.githubusercontent.com/yt-dlp/yt-dlp/2026.08.19/LICENSE'),
        ('yt-dlp-THIRD_PARTY_LICENSES.txt', 'https://raw.githubusercontent.com/yt-dlp/yt-dlp/2026.08.19/THIRD_PARTY_LICENSES.txt'),
    ):
        if not (ROOT / filename).exists():
            download(source, ROOT / filename)
    record = ROOT / 'provenance.json'
    old = json.loads(record.read_text(encoding='utf-8')) if record.exists() else {}
    old.update(provenance)
    record.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',help='Previously downloaded exact pinned FFmpeg archive')
    parser.add_argument('--replace-tool-cache',action='store_true',help='Explicitly replace this source workspace tool cache; never needed by client')
    options=parser.parse_args()
    main(options.archive,options.replace_tool_cache)
