"""缓存与当前 Qt 系列匹配的官方 WebEngine / Chromium 第三方许可资料。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from PySide6.QtCore import qVersion

ROOT = Path(__file__).resolve().parents[1]


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.hrefs.extend(value for key, value in attrs if key == 'href' and value)


def fetch(url):
    with urlopen(Request(url, headers={'User-Agent': 'StreamMediaDownloader-build-notices/1.0'}), timeout=25) as response:
        data = response.read(4 * 1024 * 1024 + 1)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError('Qt 许可页超过构建预算。')
        return data


def main():
    version = qVersion()
    series = '.'.join(version.split('.')[:2])
    base = f'https://doc.qt.io/qt-{series}/'
    source = base + 'qtwebengine-licensing.html'
    target = ROOT / 'verification' / 'browser-licenses'
    manifest = target / 'provenance.json'
    if manifest.exists():
        previous = json.loads(manifest.read_text(encoding='utf-8'))
        if previous.get('qt_version') == version and all((target / item['file']).is_file()
                and sha256((target / item['file']).read_bytes()).hexdigest() == item['sha256']
                for item in previous.get('files', [])) and len(previous.get('files', [])) > 10:
            print(f'已校验当前 Qt {version} 浏览器许可缓存：{len(previous["files"])} 页')
            return
    data = fetch(source)
    parser = Links()
    parser.feed(data.decode('utf-8'))
    urls = {source}
    for href in parser.hrefs:
        parsed = urlsplit(urljoin(source, href))
        if (parsed.scheme == 'https' and parsed.hostname == 'doc.qt.io'
                and parsed.path.startswith(f'/qt-{series}/qtwebengine-3rdparty-')
                and parsed.path.endswith('.html')):
            urls.add('https://doc.qt.io' + parsed.path)
    if not 10 <= len(urls) <= 200:
        raise ValueError('未识别到完整官方浏览器第三方许可列表。')
    target.mkdir(parents=True, exist_ok=True)
    records = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = {url: pool.submit(fetch, url) for url in sorted(urls) if url != source}
        for url in sorted(urls):
            body = data if url == source else results[url].result()
            filename = urlsplit(url).path.rsplit('/', 1)[-1]
            (target / filename).write_bytes(body)
            records.append({'file': filename, 'source': url, 'size': len(body), 'sha256': sha256(body).hexdigest()})
    manifest.write_text(json.dumps({'qt_version': version, 'source': source,
        'fetched_at': datetime.now(timezone.utc).isoformat(), 'files': records}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Qt {version} WebEngine/Chromium 官方许可资料：{len(records)} 页，{sum(item["size"] for item in records)} bytes')


if __name__ == '__main__':
    main()
