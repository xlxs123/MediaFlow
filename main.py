"""开发运行：python main.py；打包后直接双击 Windows EXE。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _run_interactive(app, window_factory, *, config_store=None, force_migration=False):
    """迁移仅发生在主窗口的异步关闭流程结束、数据目录租约释放之后。"""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QMessageBox
    from core.data_migration import DataDirectoryLease, MigrationError
    from ui.migration_dialog import MigrationDialog, MigrationRecoveryDialog, should_offer_migration
    from ui.webview2_browser import browser_profile_path
    from utils.config import ConfigStore

    config = config_store or ConfigStore()

    def migrate(first_run=False):
        app.setQuitOnLastWindowClosed(False)
        dialog = MigrationDialog(config.path.parent, target_profile=browser_profile_path(config),
                                 first_run=first_run)
        dialog.exec()
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    if force_migration or should_offer_migration(config.path.parent):
        migrate(first_run=not force_migration)
    while True:
        # 只检查事务库是否存在；耗时恢复在独立后台线程和局部事件循环中完成。
        if (config.path.parent / 'migration' / 'transactions.sqlite3').is_file():
            app.setQuitOnLastWindowClosed(False)
            recovery = MigrationRecoveryDialog(config.path.parent, browser_profile_path(config))
            recovery.exec()
            passed = recovery.passed
            recovery.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if not passed:
                return 1
        lease = DataDirectoryLease(config.path.parent)
        try:
            lease.acquire()
        except MigrationError as error:
            lease.close()
            QMessageBox.critical(None, '当前版本数据目录正在使用', str(error))
            return 1
        try:
            app.setQuitOnLastWindowClosed(True)
            window = window_factory(config)
            window.show()
            result = app.exec()
            migration_requested = bool(getattr(window, '_migration_requested', False))
        finally:
            lease.close()
        if not migration_requested:
            return result
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        migrate()


def main():
    if '--update-fetch-worker' in sys.argv:
        from core.update_transport import worker_main
        return worker_main()
    if '--update-health' in sys.argv:
        from core.update_health import run_health_cli
        return run_health_cli(sys.argv)
    if '--build-info' in sys.argv:
        import json
        from utils.build_info import runtime_identity
        identity = runtime_identity()
        index = sys.argv.index('--build-info')
        value = json.dumps(identity, ensure_ascii=False, indent=2)
        if index + 1 < len(sys.argv) and not sys.argv[index + 1].startswith('--'):
            Path(sys.argv[index + 1]).write_text(value, encoding='utf-8')
        elif sys.stdout is not None:
            print(value)
        return 0 if identity['dash_module_loaded'] else 1
    # 后台验收入口不会改变用户的真实设置，也不会访问第三方媒体。
    smoke = '--smoke-test' in sys.argv
    discovery_smoke = '--discovery-smoke' in sys.argv
    resolver_smoke = '--resolver-smoke' in sys.argv
    batch_smoke = '--batch-smoke' in sys.argv
    rou_current_smoke = '--rou-current-smoke' in sys.argv
    mediaflow_smoke = '--mediaflow-smoke' in sys.argv
    browser_environment_smoke = '--browser-environment-smoke' in sys.argv
    webview2_smoke = '--webview2-smoke' in sys.argv
    webview2_live = '--webview2-live' in sys.argv
    browser_profile_smoke = '--browser-profile-smoke' in sys.argv
    browser_profile_stage = '--browser-profile-stage' in sys.argv
    browser_capture_smoke = '--browser-capture-smoke' in sys.argv
    recovery_acceptance = '--recovery-acceptance' in sys.argv
    tool_startup_check = '--tool-startup-check' in sys.argv
    library_check = '--library-check' in sys.argv
    batch_import_check = '--batch-import-check' in sys.argv
    tracks_check = '--tracks-check' in sys.argv
    organization_check = '--organization-check' in sys.argv
    storage_check = '--storage-check' in sys.argv
    migration_check = '--migration-check' in sys.argv
    update_check = '--update-check' in sys.argv
    public_update_check = '--update-public-check' in sys.argv
    if smoke or discovery_smoke or resolver_smoke or batch_smoke or rou_current_smoke or mediaflow_smoke or browser_environment_smoke or webview2_smoke or webview2_live or browser_profile_smoke or browser_profile_stage or browser_capture_smoke or recovery_acceptance or tool_startup_check or library_check or batch_import_check or tracks_check or organization_check or storage_check or migration_check or update_check or public_update_check:
        os.environ.setdefault('QT_QPA_PLATFORM', 'windows' if '--native-smoke' in sys.argv else 'offscreen')
        # 仅测试关闭 GPU 加速，保留 Chromium sandbox、TLS 和网页安全策略。
        os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')
    from core.browser_runtime import prepare_browser_runtime
    prepare_browser_runtime()
    from PySide6.QtCore import QCoreApplication, Qt
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    from ui.main_window import MainWindow
    from ui.app_setup import configure_app

    app = QApplication(sys.argv)
    configure_app(app)
    if public_update_check:
        from tests.public_update_acceptance import run_public_update_acceptance
        return run_public_update_acceptance(app, sys.argv)
    if update_check:
        from tests.update_acceptance import run_update_acceptance
        return run_update_acceptance(app, sys.argv)
    if migration_check:
        from tests.migration_acceptance import run_migration_acceptance
        return run_migration_acceptance(app, sys.argv)
    if storage_check:
        from tests.storage_acceptance import run_storage_acceptance
        return run_storage_acceptance(app, sys.argv)
    if organization_check:
        from tests.organization_acceptance import run_organization_acceptance
        return run_organization_acceptance(app, sys.argv)
    if tracks_check:
        from tests.track_acceptance import run_track_acceptance
        return run_track_acceptance(app, sys.argv)
    if batch_import_check:
        from tests.batch_import_acceptance import run_batch_import_acceptance
        return run_batch_import_acceptance(app, sys.argv)
    if library_check:
        from tests.library_acceptance import run_library_acceptance
        return run_library_acceptance(app, sys.argv)
    if tool_startup_check:
        from tests.tool_startup_acceptance import run_tool_startup_check
        return run_tool_startup_check(app, sys.argv)
    if '--douyin-compare' in sys.argv:
        from tests.douyin_compare import main as run_douyin_compare
        return run_douyin_compare(app=app)
    if recovery_acceptance:
        from tests.recovery_acceptance import run_recovery_acceptance
        return run_recovery_acceptance(app, sys.argv)
    if '--live-release-check' in sys.argv:
        from tests.live_release_check import run_live_release_check
        return run_live_release_check(app, sys.argv)
    icon_root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    icon = icon_root / 'assets' / 'app.ico'
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    if smoke:
        from tests.gui_smoke import run_smoke
        return run_smoke(app, sys.argv)
    if discovery_smoke:
        from tests.discovery_smoke import run_discovery_smoke
        return run_discovery_smoke(app, sys.argv)
    if resolver_smoke:
        from tests.resolver_smoke import run_resolver_smoke
        return run_resolver_smoke(app, sys.argv)
    if batch_smoke:
        from tests.batch_smoke import run_batch_smoke
        return run_batch_smoke(app, sys.argv)
    if rou_current_smoke:
        from tests.rou_current_smoke import run_rou_current_smoke
        return run_rou_current_smoke(app, sys.argv)
    if mediaflow_smoke:
        from tests.mediaflow_smoke import run_mediaflow_smoke
        return run_mediaflow_smoke(app, sys.argv)
    if browser_environment_smoke:
        from tests.browser_environment_smoke import run_browser_environment_smoke
        return run_browser_environment_smoke(app, sys.argv)
    if webview2_smoke:
        from tests.webview2_smoke import run_webview2_smoke
        return run_webview2_smoke(app, sys.argv)
    if webview2_live:
        from tests.webview2_live import run_webview2_live
        return run_webview2_live(app, sys.argv)
    if browser_profile_smoke:
        from tests.browser_profile_smoke import run_browser_profile_smoke
        return run_browser_profile_smoke(app, sys.argv)
    if browser_profile_stage:
        from tests.browser_profile_smoke import run_browser_profile_stage
        return run_browser_profile_stage(app, sys.argv)
    if browser_capture_smoke:
        from tests.browser_capture_smoke import run_browser_capture_smoke
        return run_browser_capture_smoke(app, sys.argv)
    return _run_interactive(app, MainWindow, force_migration='--data-migration' in sys.argv)


if __name__ == '__main__':
    raise SystemExit(main())
