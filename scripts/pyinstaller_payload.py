"""Bind the archive actually selected by the pinned Windows bootloader.

This parses data only. It never imports archive members, extracts files, runs a
PE or substitutes for native Authenticode verification. Full-file selection is
intentional: truncating a signed carrier would disagree with the bootloader.
"""
from __future__ import annotations

import hashlib
import struct


PYINSTALLER_VERSION = '6.22.3'
BOOTLOADER_COMMIT = 'ecd7993d65c43b254f4e3f718e36d5b4a70a3ed5'
MAGIC = b'MEI\014\013\012\013\016'
COOKIE = struct.Struct('!8sIIII64s')
TOC_ENTRY = struct.Struct('!IIIIBc')
MAX_CARRIER = 600 * 1024 * 1024
MAX_MEMBERS = 20000


class PayloadError(RuntimeError):
    pass


def selected_archive(data):
    """Observe the rightmost cookie and bounded PKG/TOC/PYZ, without execution.

    pyi_utils.c:181-240 searches overlapping 8192-byte chunks from physical EOF
    and returns the rightmost match. bytes.rfind has the same selected offset.
    Offset zero is rejected, matching the native API's zero/not-found result.
    The original CI archive is trusted separately; this is not a general PKG
    validator or proof of compatibility with a different bootloader version.
    """
    if type(data) is not bytes or not 0 < len(data) <= MAX_CARRIER:
        raise PayloadError('PyInstaller carrier is absent or exceeds its bound.')
    position = data.rfind(MAGIC)
    if position <= 0 or position + COOKIE.size > len(data):
        raise PayloadError('The bootloader-selected CArchive cookie is absent or incomplete.')
    _, length, toc_offset, toc_length, python_version, python_library = COOKIE.unpack_from(data, position)
    end = position + COOKIE.size
    start = end - length
    toc_start = start + toc_offset
    if (start < 0 or length < COOKIE.size or toc_length < TOC_ENTRY.size
            or not start <= toc_start < position or toc_start + toc_length != position
            or not python_library.split(b'\0', 1)[0]):
        raise PayloadError('The selected CArchive has unsupported PKG/TOC bounds.')
    cursor, count, pyz = toc_start, 0, []
    while cursor < position:
        if cursor + TOC_ENTRY.size > position or count >= MAX_MEMBERS:
            raise PayloadError('The selected CArchive TOC is incomplete or exceeds its bound.')
        size, offset, stored, expanded, compressed, kind = TOC_ENTRY.unpack_from(data, cursor)
        if (size < TOC_ENTRY.size + 1 or size % 16 or cursor + size > position
                or compressed not in (0, 1) or offset + stored > toc_offset):
            raise PayloadError('The selected CArchive contains unsupported member bounds.')
        raw_name = data[cursor + TOC_ENTRY.size:cursor + size]
        name, separator, padding = raw_name.partition(b'\0')
        if not name or not separator or any(padding):
            raise PayloadError('The selected CArchive contains unsupported member-name padding.')
        # The writer permits repeated script/module/option names. Preserve and
        # hash the raw ordered TOC; do not merge it into a name-keyed dictionary.
        if kind == b'z':
            if compressed or not stored or expanded != stored:
                raise PayloadError('The selected PYZ must be directly addressable by the importer.')
            pyz.append(dict(offset=start + offset, size=stored,
                sha256=hashlib.sha256(memoryview(data)[start + offset:start + offset + stored]).hexdigest()))
        count += 1
        cursor += size
    if cursor != position or len(pyz) != 1:
        raise PayloadError('The selected archive must contain exactly one bounded PYZ.')
    return dict(pyinstaller_version=PYINSTALLER_VERSION, bootloader_commit=BOOTLOADER_COMMIT,
        selector='rightmost-cookie-from-physical-EOF', cookie_offset=position,
        archive_start=start, archive_end=end, archive_size=length,
        archive_sha256=hashlib.sha256(memoryview(data)[start:end]).hexdigest(),
        toc_offset=toc_start, toc_size=toc_length,
        toc_sha256=hashlib.sha256(memoryview(data)[toc_start:position]).hexdigest(),
        members=count, pyz=pyz,
        python_version=python_version,
        python_library_sha256=hashlib.sha256(python_library).hexdigest())


def archive_unchanged(before, after):
    """Require identical runtime selection, not merely an intact old prefix."""
    original, returned = selected_archive(before), selected_archive(after)
    if original['archive_end'] != len(before):
        raise PayloadError('The original CI onefile archive must end at the unsigned carrier EOF.')
    if original != returned:
        raise PayloadError('Signing changed the bootloader-selected PKG/TOC/PYZ identity; result refused.')
    return dict(passed=True, **original,
        scope='Selected archive identity only; byte derivation and native production trust remain mandatory.')
