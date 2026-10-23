"""Packaged EXE acceptance through the real MainWindow, detector and download queue."""
from dataclasses import asdict
import ctypes
import json
from pathlib import Path
import subprocess
import time

from PySide6.QtCore import QTimer
from core.tool_manager import APP_DIR, ToolInfo, get_tool_manager
from core.process_tree import NO_WINDOW
from ui.main_window import MainWindow
from utils.build_info import runtime_identity
from utils.config import ConfigStore


def run_tool_startup_check(app, args):
    def option(flag, default=''):
        return args[args.index(flag) + 1] if flag in args else default

    directory = Path(option('--tool-startup-check')).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    case = option('--tool-case', 'startup')
    store = ConfigStore(option('--tool-config') or directory / 'config.json')
    if case == 'stale':
        values = {}
        for name in ('yt_dlp', 'ffmpeg', 'ffprobe'):
            values.update({name + '_path': str(directory / 'nonexistent' / (name + '.exe')),
                           name + '_mode': 'custom'})
        store.save(values)
    if case in ('timeout', 'shutdown'):
        fixture = Path(option('--tool-fixture')).resolve()
        if not fixture.is_file():
            raise RuntimeError('Pass the local hanging fixture executable.')
        store.save({'yt_dlp_path': str(fixture), 'yt_dlp_mode': 'custom'})
    report = {'case': case, 'runtime': runtime_identity(), 'checks': [], 'passed': False,
              'release_directory': str(APP_DIR), 'cwd': str(Path.cwd()), 'protocols': []}
    window, fixture = None, None
    ticks = []
    timer = QTimer()
    timer.setInterval(25)
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    app.setQuitOnLastWindowClosed(False)

    def wait_until(predicate, seconds=35):
        deadline = time.monotonic() + seconds
        while not predicate():
            app.processEvents()
            time.sleep(0.005)
            if time.monotonic() > deadline:
                raise RuntimeError('Acceptance exceeded its bounded wait.')
        app.processEvents()

    def check(condition, description):
        if not condition:
            raise AssertionError(description)
        report['checks'].append(description)

    def check_fixture_child():
        path = Path(option('--tool-fixture') + '.pid')
        if path.is_file():
            pid = int(path.read_text())
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x100000, False, pid)
            try:
                check(not handle or kernel.WaitForSingleObject(handle, 1000) == 0,
                      '检测工具创建的子进程已退出')
            finally:
                if handle:
                    kernel.CloseHandle(handle)

    started = time.monotonic()
    try:
        window = MainWindow(store)
        window.showMinimized()
        window.detect_tools()
        detector = window.detector
        window.detect_tools()
        check(window.detector is detector, '重复点击不会创建第二个检测任务')
        timer.start()
        if case == 'shutdown':
            before = time.monotonic()
            while time.monotonic() - before < 0.4:
                app.processEvents()
                time.sleep(0.01)
            window.close()
            wait_until(lambda: window.detector is None, 5)
            report['close_seconds'] = round(time.monotonic() - before - 0.4, 3)
            check(report['close_seconds'] < 4, '关闭软件会取消检测并清理进程')
            check_fixture_child()
        else:
            wait_until(lambda: window.detector is None)
            report['detection_elapsed_seconds'] = round(time.monotonic() - started, 3)
            report['detection_ui_ticks'] = len(ticks)
            report['detection_max_ui_tick_gap_seconds'] = round(max((b-a for a, b in zip(ticks, ticks[1:])), default=0), 3)
            infos = {'yt-dlp': window.yt_info, 'ffmpeg': window.ff_info, 'ffprobe': window.probe_info}
            report['tools'] = {name: asdict(info) for name, info in infos.items()}
            check(all(info.found for info in infos.values()), '三个工具均已运行版本命令并可用')
            check(all(Path(info.path).parent == APP_DIR / 'tools' for info in infos.values()), '默认自动使用当前发布目录的 tools')
            check(window.tool_table.rowCount() == 3, '设置页显示三项工具的路径、状态、版本和最近错误')
            if case == 'stale':
                check(all(store.load()[name + '_mode'] == 'auto' for name in ('yt_dlp', 'ffmpeg', 'ffprobe')),
                      '三个失效的自定义路径均已自动回退并保存自动模式')
            if case == 'timeout':
                check(any(a['status'] == 'timeout' for a in window.yt_info.attempts), '真实挂起进程被识别为超时后自动回退')
                check(len(ticks) > 100 and window.start_button.isEnabled(), '八秒超时期间界面事件和下载输入仍正常响应')
                check_fixture_child()
            if case == 'protocols':
                from tests.hls_fixture import HlsFixture, FIXTURE_HEADERS
                import tests.hls_fixture as fixture_module
                fixture_module.ROOT = APP_DIR
                fixture = HlsFixture(directory / 'media')
                fixture.generate()
                fixture.start()
                window.output_directory.setText(str(directory / 'downloads'))
                manager = get_tool_manager()
                detect = manager.detect
                calls = []
                def track(name, *params, **options):
                    calls.append(name)
                    return detect(name, *params, **options)
                manager.detect = track
                try:
                    for protocol, path, headers in (
                        ('Direct', '/plain/direct.mp4', {}),
                        ('HLS', '/plain/index.m3u8', {}),
                        ('DASH', '/dash/endpoint', FIXTURE_HEADERS)):
                        # Simulate a stale failed UI result; retain the actual bundled
                        # backend. This proves no global detector gate, not a new
                        # yt-dlp-free downloader (the existing backend needs yt-dlp).
                        window.yt_info = ToolInfo(error='模拟先前检测超时', status='timeout')
                        if protocol == 'Direct':
                            window.ff_info = ToolInfo(error='模拟未安装非必需封装工具', status='missing')
                        else:
                            window.ff_info = infos['ffmpeg']
                        window.parse_text(fixture.curl('cmd', path, headers) if headers else fixture.base + path)
                        window.filename.setText(protocol.lower())
                        count = len(calls)
                        job = window.add_current_to_queue()
                        check(job is not None, protocol + ' 在旧检测失败状态下仍可加入队列')
                        window.start_queue()
                        wait_until(lambda: job.worker is None and job.state in ('completed', 'failed'), 100)
                        result = job.result or {}
                        check(result.get('success') is True, protocol + ' 经现有下载管线完成并通过媒体验证')
                        output = Path(result['files'][0])
                        decoded = subprocess.run([infos['ffmpeg'].path, '-v', 'error', '-i', str(output),
                            '-f', 'null', '-'], shell=False, capture_output=True, timeout=30, creationflags=NO_WINDOW)
                        check(decoded.returncode == 0 and not decoded.stderr, protocol + ' 完整文件解码无错误')
                        invoked = calls[count:]
                        if protocol == 'Direct':
                            check('ffmpeg' not in invoked, '无需封装的 Direct 下载不要求 FFmpeg 检测成功')
                        report['protocols'].append({'protocol': protocol, 'success': True,
                            'required_tools': invoked, 'files': result['files'],
                            'validation': result.get('output_validation'), 'decode_exit_code': decoded.returncode,
                            'bytes': output.stat().st_size, 'backend': 'existing MediaBridge + yt-dlp'})
                finally:
                    manager.detect = detect
            window.settings_toggle.setChecked(True)
            settled = time.monotonic() + 0.2
            wait_until(lambda: time.monotonic() >= settled, 2)
            window.grab().save(str(directory / 'settings.png'))
        report['passed'] = True
    except Exception as error:
        report['error'] = str(error)
    finally:
        timer.stop()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['ui_ticks'] = len(ticks)
        report['max_ui_tick_gap_seconds'] = round(max((b-a for a, b in zip(ticks, ticks[1:])), default=0), 3)
        if window:
            window.queue.shutdown()
            if window.detector:
                window.detector.cancel()
            wait_until(lambda: window.detector is None and not window.queue.active_count and not window.thumbnails.busy, 15)
            window.close()
            app.processEvents()
        if fixture:
            fixture.close()
        (directory / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['passed'] else 1
