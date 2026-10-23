"""完整文件哈希缓存；只存本地路径、stat 身份和 SHA-256。"""
import json
from pathlib import Path
import re
import sqlite3


class StorageCacheError(RuntimeError):
    pass


class HashCache:
    def __init__(self, path):
        self.connection = None
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, timeout=3)
            if self.connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise StorageCacheError('哈希缓存损坏，原文件已保留；本次改用完整重新计算。')
            self.connection.execute('''CREATE TABLE IF NOT EXISTS hashes (
                path TEXT PRIMARY KEY, identity TEXT NOT NULL, sha256 TEXT NOT NULL)''')
        except (sqlite3.Error, OSError, StorageCacheError):
            self.close()
            raise StorageCacheError('无法读取哈希缓存，原文件已保留；本次改用完整重新计算。') from None

    def get(self, key, identity):
        try:
            row = self.connection.execute('SELECT identity,sha256 FROM hashes WHERE path=?', (key,)).fetchone()
            if row and json.loads(row[0]) == list(identity) and re.fullmatch(r'[0-9a-f]{64}', row[1]):
                return row[1]
        except (sqlite3.Error, ValueError, TypeError):
            pass
        return ''

    def put(self, key, identity, digest):
        try:
            self.connection.execute('INSERT OR REPLACE INTO hashes VALUES (?,?,?)',
                                    (key, json.dumps(list(identity)), digest))
            self.connection.commit()
        except sqlite3.Error:
            raise StorageCacheError('哈希缓存保存失败；本次已计算的内容验证仍有效。') from None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
