"""从官方链接准备可携带的外部工具，校验发布方提供的 SHA256。"""
from pathlib import Path
import hashlib
import json
import re
import shutil
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent


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


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    provenance = {}
    yt = ROOT / 'yt-dlp.exe'
    if not yt.exists():
        base = 'https://github.com/yt-dlp/yt-dlp/releases/latest/download/'
        sums = ROOT / 'yt-dlp-SHA2-256SUMS.txt'
        download(base + 'SHA2-256SUMS', sums)
        match = re.search(r'^([a-fA-F0-9]{64})\s+\*?yt-dlp\.exe\s*$', sums.read_text(), re.M)
        if not match:
            raise RuntimeError('发布方校验清单中未找到 yt-dlp.exe。')
        temporary = yt.with_suffix('.download')
        download(base + 'yt-dlp.exe', temporary)
        provenance['yt-dlp.exe'] = {'source': base + 'yt-dlp.exe', 'sha256': verify(temporary, match.group(1))}
        temporary.replace(yt)
        print('yt-dlp.exe 已准备，SHA256 验证通过。', flush=True)
    ffmpeg = ROOT / 'ffmpeg.exe'
    ffprobe = ROOT / 'ffprobe.exe'
    if not ffmpeg.exists() or not ffprobe.exists():
        base = 'https://www.gyan.dev/ffmpeg/builds/'
        archive = ROOT / 'ffmpeg-release-essentials.zip'
        sums = ROOT / 'ffmpeg-SHA256.txt'
        download(base + 'ffmpeg-release-essentials.zip.sha256', sums)
        match = re.search(r'\b([a-fA-F0-9]{64})\b', sums.read_text())
        if not match:
            raise RuntimeError('FFmpeg SHA256 格式无效。')
        download(base + archive.name, archive)
        digest = verify(archive, match.group(1))
        with zipfile.ZipFile(archive) as bundle:
            for filename in ('ffmpeg.exe', 'ffprobe.exe'):
                member = next(info for info in bundle.infolist() if info.filename.endswith('/bin/' + filename))
                with bundle.open(member) as source, (ROOT / filename).open('wb') as target:
                    shutil.copyfileobj(source, target)
                provenance[filename] = {'source': base + archive.name, 'archive_sha256': digest}
                with (ROOT / filename).open('rb') as handle:
                    hasher = hashlib.sha256()
                    for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                        hasher.update(chunk)
                    provenance[filename]['sha256'] = hasher.hexdigest()
            for info in bundle.infolist():
                if info.filename.lower().endswith(('/license', '/license.txt', '/readme.txt')):
                    with bundle.open(info) as source, (ROOT / ('FFmpeg-' + Path(info.filename).name)).open('wb') as target:
                        shutil.copyfileobj(source, target)
        print('ffmpeg.exe / ffprobe.exe 已准备，发行包 SHA256 验证通过。', flush=True)
    for filename, source in (
        ('yt-dlp-LICENSE.txt', 'https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/LICENSE'),
        ('yt-dlp-THIRD_PARTY_LICENSES.txt', 'https://raw.githubusercontent.com/yt-dlp/yt-dlp/master/THIRD_PARTY_LICENSES.txt'),
    ):
        if not (ROOT / filename).exists():
            download(source, ROOT / filename)
    record = ROOT / 'provenance.json'
    old = json.loads(record.read_text(encoding='utf-8')) if record.exists() else {}
    old.update(provenance)
    record.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
