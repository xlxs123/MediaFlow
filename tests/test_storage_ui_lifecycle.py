"""Exercise the real storage fixture's failure cleanup without crashing the runner."""
import gc
import threading
import unittest
from unittest.mock import patch

import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication

import tests.test_storage_ui as storage_ui


class _ScanFailure(RuntimeError):
    pass


class StorageUILifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        storage_ui.StorageUITests.setUpClass()

    def fixture(self):
        fixture = storage_ui.StorageUITests(
            'test_actual_many_files_scanned_in_background_and_rendered_paginated')
        fixture.setUp()
        self.addCleanup(self.dispose, fixture)
        return fixture

    def dispose(self, fixture):
        # Also clean up safely when this regression is run against the old code.
        for timer in getattr(fixture, '_lifecycle_test_timers', []):
            if shiboken6.isValid(timer):
                timer.stop()
                timer.deleteLater()
                QCoreApplication.sendPostedEvents(timer, QEvent.Type.DeferredDelete)
        if not fixture.service.is_closed:
            fixture.service.shutdown()
        fixture.wait(lambda: fixture.service.is_closed
                     and not fixture.service._worker._thread.is_alive())
        if shiboken6.isValid(fixture.panel):
            fixture.panel.close()
            fixture.panel.deleteLater()
            QCoreApplication.sendPostedEvents(fixture.panel, QEvent.Type.DeferredDelete)
        fixture.temp.cleanup()

    def fail_actual_scan(self, fixture):
        timers = []
        constructor = storage_ui.QTimer

        def capture(*args, **kwargs):
            timer = constructor(*args, **kwargs)
            timers.append(timer)
            return timer

        # A strong reference permits deterministic assertions on the old active
        # timer, rather than letting a background GC crash this test process.
        fixture._lifecycle_test_timers = timers
        with patch.object(storage_ui, 'QTimer', side_effect=capture), \
                patch.object(fixture, 'scan', side_effect=_ScanFailure('synthetic scan failure')):
            with self.assertRaises(_ScanFailure):
                fixture.test_actual_many_files_scanned_in_background_and_rendered_paginated()
        self.assertEqual(len(timers), 1)
        return timers[0]

    def test_actual_scan_exception_stops_and_parents_the_heartbeat(self):
        fixture = self.fixture()
        timer = self.fail_actual_scan(fixture)
        self.assertFalse(timer.isActive(), 'A failed scan must not leave a running Qt timer')
        self.assertIs(timer.parent(), fixture.panel)

    def test_original_teardown_completes_panel_deferred_delete(self):
        fixture = self.fixture()
        fixture.tearDown()
        self.assertFalse(shiboken6.isValid(fixture.panel),
                         'processEvents alone does not deliver DeferredDelete')
        self.assertFalse(fixture.service._worker._thread.is_alive())

    def test_failed_fixture_destroys_timer_on_owner_thread_before_worker_gc(self):
        fixture = self.fixture()
        timer = self.fail_actual_scan(fixture)
        owner = threading.get_native_id()
        destroyed_on = []
        timer.destroyed.connect(lambda *_args: destroyed_on.append(threading.get_native_id()),
                                Qt.ConnectionType.DirectConnection)
        fixture.tearDown()
        self.assertFalse(shiboken6.isValid(timer))
        self.assertEqual(destroyed_on, [owner])
        worker = threading.Thread(target=gc.collect)
        worker.start()
        worker.join(timeout=15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(destroyed_on, [owner])


if __name__ == '__main__':
    unittest.main()
