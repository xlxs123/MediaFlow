"""在 PyInstaller Analysis 前生成写入 EXE 的构建标识及 Windows 版本资源。"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.branding import APP_VERSION
from utils.build_info import DASH_FEATURE_REVISION, source_digest


def main():
    now = datetime.now(timezone.utc)
    digest = source_digest(ROOT)
    stamp = {'app_version': APP_VERSION, 'build_id': now.strftime('%Y%m%dT%H%M%SZ') + '-' + digest[:12],
             'built_at_utc': now.isoformat(), 'source_sha256': digest, 'dash_feature_revision': DASH_FEATURE_REVISION}
    (ROOT / 'assets/build.json').write_text(json.dumps(stamp, indent=2), encoding='utf-8')
    parts = tuple(int(x) for x in APP_VERSION.split('.')) + (0,)
    path = ROOT / 'assets/version_info.txt'
    import re
    text = path.read_text(encoding='utf-8')
    text = re.sub(r'(filevers|prodvers)=\([^)]*\)', lambda m: m[1] + '=' + repr(parts), text)
    text = re.sub(r"StringStruct\('(FileVersion|ProductVersion)', '[^']*'\)",
                  lambda m: f"StringStruct('{m[1]}', '{APP_VERSION}')", text)
    path.write_text(text, encoding='utf-8')
    print(json.dumps(stamp))


if __name__ == '__main__':
    main()
