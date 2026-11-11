"""先前完整读取的 SHA-256 记录；相同 stat 身份仍需内容核对。"""
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
        self.put_many(((key, identity, digest),))

    def put_many(self, records):
        """批量持久化已确认的哈希，先准备参数再获取写锁。"""
        parameters = [(key, json.dumps(list(identity)), digest) for key, identity, digest in records]
        if not parameters:
            return
        try:
            with self.connection:
                self.connection.execute('BEGIN IMMEDIATE')
                self.connection.executemany('INSERT OR REPLACE INTO hashes VALUES (?,?,?)', parameters)
        except sqlite3.Error:
            raise StorageCacheError('哈希缓存保存失败；本次已计算的内容验证仍有效。') from None

    def discard_many(self, records):
        # Do not remove a different digest that a concurrent scan has already saved.
        parameters = [(key, json.dumps(list(identity)), digest) for key, identity, digest in records]
        try:
            with self.connection:
                self.connection.executemany('DELETE FROM hashes WHERE path=? AND identity=? AND sha256=?', parameters)
        except sqlite3.Error:
            raise StorageCacheError('无法清除已失效的哈希缓存。') from None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
