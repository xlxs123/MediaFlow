"""Benign archive metadata fixtures, never imported, extracted or executed.

The PYZ-labelled member contains plain test data, not Python code. These inputs
test archive selection/interval binding, not PYZ validity or signature trust.
"""
import struct

from scripts.pyinstaller_payload import COOKIE, MAGIC, TOC_ENTRY


def archive_data(prefix=b'plain carrier prefix\0', payload=b'data never executed; padded....', extra_types=()):
    payload = payload.ljust(32, b'.')
    name = b'PYZ.pyz\0'
    size = (TOC_ENTRY.size + len(name) + 15) // 16 * 16
    toc = TOC_ENTRY.pack(size, 0, len(payload), len(payload), 0, b'z')
    toc += name.ljust(size - TOC_ENTRY.size, b'\0')
    for kind in extra_types:
        name = b'bootstrap\0'
        size = (TOC_ENTRY.size + len(name) + 15) // 16 * 16
        toc += TOC_ENTRY.pack(size, 0, len(payload), len(payload), 0, kind)
        toc += name.ljust(size - TOC_ENTRY.size, b'\0')
    length = len(payload) + len(toc) + COOKIE.size
    return prefix + payload + toc + COOKIE.pack(MAGIC, length, len(payload), len(toc),
        314, b'python314.dll')
