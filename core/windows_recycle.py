"""有总时限的 Windows 回收站辅助进程；绝不调用 unlink 或永久删除。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time
import uuid

from core.process_tree import ProcessTree, NO_WINDOW
from core.tool_manager import application_directory


class RecycleError(RuntimeError):
    pass


def read_receipt(path, source, proof, nonce):
    try:
        file = Path(path)
        if file.is_symlink() or file.stat().st_size > 16384:
            return None
        data = json.loads(file.read_text(encoding='utf-8'))
        if (data.get('recycled') is not True or data.get('nonce') != nonce
                or os.path.normcase(data.get('path', '')) != os.path.normcase(str(source))
                or data.get('sha256') != proof['sha256'] or data.get('size') != proof['size']
                or not int(data.get('pre_flags', 0)) & 0x80):
            return None
        root = Path(source).anchor
        bin_root = os.path.normcase(str(Path(root) / '$Recycle.Bin')) + os.sep
        if not os.path.normcase(data.get('bin_path', '')).startswith(bin_root):
            return None
        return data
    except (OSError, ValueError, TypeError, KeyError):
        return None


def recycle_file(source, proof, receipt_path, *, nonce=None, timeout=60, cancel_event=None):
    if os.name != 'nt':
        raise RecycleError('当前平台不能安全调用 Windows 回收站，未删除文件。')
    helper = application_directory() / 'tools/MediaFlow.Recycle.exe'
    if not helper.is_file():
        raise RecycleError('回收站辅助程序缺失，请保留完整发布文件夹；未删除文件。')
    source = Path(source).absolute()
    receipt_path = Path(receipt_path)
    nonce = nonce or uuid.uuid4().hex
    if receipt_path.exists():
        raise RecycleError('回收凭据路径已经存在，请重新预览。')
    process = subprocess.Popen([str(helper), str(source), proof['sha256'], str(receipt_path), nonce],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=NO_WINDOW)
    tree = ProcessTree(process)
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        # 不在回收过程中强行因用户取消而终止；取消只停止后续文件。
        while process.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                tree.terminate()
                break
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            tree.terminate()
        tree.close()
    receipt = read_receipt(receipt_path, source, proof, nonce)
    if receipt and not source.exists():
        return receipt
    if timed_out:
        raise RecycleError('回收站操作超时，未执行永久删除；请重新扫描以确认文件状态。')
    raise RecycleError('无法安全移入回收站（可能是文件占用、权限、非本地固定磁盘或回收站不可用）。未改为永久删除，请取消并检查后重试。')
