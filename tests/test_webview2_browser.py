"""Pipe transport and native CDP generation boundaries, without a real browser."""
from __future__ import annotations

import json
import io
import os
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from core.webview2_observer import WebView2NetworkObserver, _NativeCdpSocket
from core.media_discovery import MediaDiscoveryService
from ui.webview2_browser import (_PipeHost, WebView2Page, select_browser_backend,
                                MAX_LINE_BYTES, MAX_REQUEST_LINE_BYTES, response_line_limit)


class FakePage(QObject):
    ready = Signal()
    cdpMessage = Signal(dict)
    backendError = Signal(str)

    def __init__(self):
        super().__init__()
        self.is_ready = True
        self.page_id = 'local-native-page'
        self.commands = []

    def send_cdp(self, message, callback):
        self.commands.append((message, callback))
        return True

    def answer(self, method, result=None, ok=True, *, session='', request_id=None):
        message, callback = next(item for item in self.commands if item[0]['method'] == method
            and item[0].get('sessionId', '') == session
            and (request_id is None or item[0]['params'].get('requestId') == request_id))
        callback({'id': message['id'], 'result': result or {}}, ok)


class NativeObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.page = FakePage()
        self.observer = WebView2NetworkObserver(self.page)
        self.observer.start()
        self.app.processEvents()

    def tearDown(self):
        self.observer.stop()

    def event(self, method, params, session=''):
        self.page.cdpMessage.emit({'method': method, 'params': params, 'sessionId': session})

    def attach_worker(self, session='worker', kind='worker'):
        self.event('Target.attachedToTarget', {'sessionId': session, 'targetInfo': {'type': kind}})
        self.page.answer('Network.enable', session=session)

    def worker_request(self, request_id, session='worker', url='https://cdn.example/opaque?sign=a%2Bb'):
        self.event('Network.requestWillBeSent', {'requestId': request_id, 'wallTime': time.time(),
            'type': 'Fetch', 'request': {'url': url, 'method': 'GET',
                'headers': {'User-Agent': 'Worker Initial', 'Referer': 'https://page.example/watch'}}}, session)

    def worker_response(self, request_id, session='worker', *, has_extra=True,
                        url='https://cdn.example/opaque?sign=a%2Bb', mime='application/octet-stream'):
        params = {'requestId': request_id, 'type': 'Fetch',
            'response': {'url': url, 'status': 206, 'mimeType': mime, 'headers': {}}}
        if has_extra is not None:
            params['hasExtraInfo'] = has_extra
        self.event('Network.responseReceived', params, session)

    def root_worker_extra(self, request_id, *, response_first=False):
        headers = {'User-Agent': 'Worker Wire', 'Referer': 'https://page.example/watch',
            'Range': 'bytes=0-', 'Cookie': 'synthetic-session=worker-unit-secret',
            'Authorization': 'Bearer worker-unit-authorization'}
        events = [('Network.requestWillBeSentExtraInfo', {'requestId': request_id, 'headers': headers}),
                  ('Network.responseReceivedExtraInfo', {'requestId': request_id, 'statusCode': 206,
                      'headers': {'Content-Type': 'video/mp4', 'Content-Length': '4000000'}})]
        for method, params in reversed(events) if response_first else events:
            self.event(method, params)
        return {name.lower(): value for name, value in headers.items()}

    def test_network_must_be_enabled_before_ready_and_navigation(self):
        ready = []
        self.observer.ready.connect(lambda: ready.append(True))
        self.assertFalse(self.observer._enabled)
        self.page.answer('Network.enable')
        self.assertTrue(self.observer._enabled)
        self.assertEqual(ready, [True])
        self.assertIn('进程管道', self.observer.network_diagnostics()['backend'])

    def test_network_enable_failure_does_not_emit_ready(self):
        ready = []
        self.observer.ready.connect(lambda: ready.append(True))
        self.page.answer('Network.enable', ok=False)
        self.assertEqual(ready, [])
        self.assertFalse(self.observer._active)

    def test_opaque_fetch_and_xhr_media_reach_existing_candidate_analyser(self):
        service = MediaDiscoveryService()
        self.observer.observation.connect(service.consume_network)
        self.page.answer('Network.enable')
        hls = '#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:9,\nopaque-segment.png\n#EXT-X-ENDLIST\n'
        dash = '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"><Period><AdaptationSet mimeType="video/mp4"><Representation id="v1" width="1280" height="720" bandwidth="2000000"/></AdaptationSet></Period></MPD>'
        cases = (('Fetch', 'video/mp4', 'Direct', None), ('XHR', 'audio/mpeg', 'Direct', None),
                 ('Fetch', 'application/vnd.apple.mpegurl', 'HLS', hls), ('XHR', 'application/dash+xml', 'DASH', dash))
        headers = {'Referer': 'https://page.example/watch', 'User-Agent': 'Edge Fixture',
                   'Range': 'bytes=0-', 'Cookie': 'synthetic=unit-only', 'Authorization': 'Bearer unit-only'}
        for index, (resource, mime, protocol, body) in enumerate(cases):
            with self.subTest(resource=resource, mime=mime):
                request_id = 'opaque-' + str(index)
                url = 'https://cdn.example/opaque-' + str(index) + '?sign=a%2Bb&token=one&token=two'
                wire = {'requestId': request_id, 'headers': headers}
                # Cover real request ExtraInfo arriving before and after the response.
                if index % 2 == 0:
                    self.event('Network.requestWillBeSentExtraInfo', wire)
                self.event('Network.requestWillBeSent', {'requestId': request_id, 'wallTime': time.time(),
                    'type': resource, 'frameId': 'main', 'request': {'url': url, 'method': 'GET', 'headers': {}}})
                self.event('Network.responseReceived', {'requestId': request_id, 'type': resource, 'hasExtraInfo': True,
                    'response': {'url': url, 'status': 200, 'mimeType': 'application/octet-stream', 'headers': {}}})
                self.assertFalse(any(candidate.url == url for candidate in service.candidates.values()))
                self.event('Network.responseReceivedExtraInfo', {'requestId': request_id, 'statusCode': 200,
                    'headers': {'Content-Type': mime, 'Content-Length': str(len(body) if body else 4000000)}})
                if index % 2:
                    self.event('Network.requestWillBeSentExtraInfo', wire)
                if body:
                    self.event('Network.loadingFinished', {'requestId': request_id, 'encodedDataLength': len(body)})
                    self.page.answer('Network.getResponseBody', {'body': body, 'base64Encoded': False}, request_id=request_id)
                candidate = next(candidate for candidate in service.candidates.values() if candidate.url == url)
                self.assertEqual(candidate.media_type, protocol)
                self.assertEqual(candidate.content_type, mime)
                self.assertTrue(candidate.context_complete)
                self.assertTrue(candidate.confirmed)
                self.assertEqual(candidate.request_context.url, url)
                self.assertEqual(candidate.request_context.headers, {key.lower(): value for key, value in headers.items()})
                self.assertNotIn('origin', candidate.request_context.headers)
                if protocol == 'HLS':
                    self.assertEqual(candidate.manifest_type, 'Media')
                    self.assertTrue(any(resource['role'] == 'Media Segment' and resource['uri'] == 'opaque-segment.png'
                                        for resource in candidate.metadata['manifest']['resources']))
        self.assertEqual(self.observer.network_diagnostics()['counts']['media_requests'], len(cases))

    def test_worker_media_is_observed_without_inventing_a_dom_frame(self):
        self.page.answer('Network.enable')
        service = MediaDiscoveryService()
        self.observer.observation.connect(service.consume_network)
        seen = []
        self.observer.observation.connect(seen.append)
        for kind in ('worker', 'shared_worker', 'service_worker'):
            with self.subTest(kind=kind):
                self.event('Target.attachedToTarget', {'sessionId': kind, 'targetInfo': {'type': kind}})
                self.page.answer('Network.enable', session=kind)
                self.event('Runtime.executionContextCreated', {'context': {'id': 17, 'auxData': {
                    'isDefault': True, 'frameId': 'not-a-worker-frame'}}}, kind)
                self.observer.scan_dom()
                commands = [item[0] for item in self.page.commands if item[0].get('sessionId') == kind]
                self.assertIn('Runtime.runIfWaitingForDebugger', [item['method'] for item in commands])
                self.assertFalse(any(item['method'].startswith('Page.') or item['method'] == 'Runtime.evaluate'
                                     for item in commands))
                self.assertNotIn((kind, 17), self.observer._contexts)
                url = 'https://cdn.example/' + kind + '/opaque?sign=unchanged%2Bvalue'
                self.event('Network.requestWillBeSent', {'requestId': 'worker-media', 'wallTime': time.time(), 'type': 'Fetch',
                    'request': {'url': url, 'method': 'GET', 'headers': {'User-Agent': 'Worker Fixture'}}}, kind)
                self.event('Network.responseReceived', {'requestId': 'worker-media', 'type': 'Fetch', 'hasExtraInfo': False,
                    'response': {'url': url, 'status': 206, 'mimeType': '',
                                 'headers': {'Content-Type': 'video/mp4', 'Content-Length': '4000000'}}}, kind)
                candidate = next(candidate for candidate in service.candidates.values() if candidate.url == url)
                # 206 缺少完整 Content-Range/正文证据，只保留为分片候选。
                self.assertEqual(candidate.media_type, 'Segment')
                self.assertTrue(candidate.metadata.get('fragment_only'))
                self.assertEqual(candidate.frame_id, '')
                self.assertEqual(seen[-1].frame_id, '')
                self.assertEqual(seen[-1].request_id, kind + ':worker-media')
                self.assertTrue(candidate.context_complete)
        self.assertEqual(self.observer.network_diagnostics()['counts']['media_requests'], 3)

    def test_worker_network_failure_resumes_guest_without_stopping_main_observer(self):
        self.page.answer('Network.enable')
        self.event('Target.attachedToTarget', {'sessionId': 'worker', 'targetInfo': {'type': 'worker'}})
        self.page.answer('Network.enable', ok=False, session='worker')
        commands = [item[0] for item in self.page.commands if item[0].get('sessionId') == 'worker']
        self.assertEqual([item['method'] for item in commands].count('Runtime.runIfWaitingForDebugger'), 1)
        self.assertTrue(self.observer._active)
        self.assertTrue(self.observer._enabled)
        self.assertEqual(self.observer.network_diagnostics()['counts']['target_enable_failed'], 1)

    def test_root_worker_extra_before_request_waits_for_confirmed_unique_response(self):
        self.page.answer('Network.enable')
        service = MediaDiscoveryService()
        self.observer.observation.connect(service.consume_network)
        for index, kind in enumerate(('worker', 'shared_worker', 'service_worker')):
            with self.subTest(kind=kind):
                self.attach_worker(kind, kind)
                request_id = 'early-' + kind
                url = 'https://cdn.example/' + kind + '/opaque?sign=a%2Bb&token=one&token=two'
                expected_headers = self.root_worker_extra(request_id)
                root = self.observer._store.get('', request_id)
                self.assertFalse(root.hops)
                self.worker_request(request_id, kind, url)
                child = self.observer._store.get(kind, request_id)
                self.assertFalse(child.extras)
                self.assertFalse(self.observer._store.snapshot(child).context_complete)
                self.assertIs(self.observer._store.get('', request_id), root)
                self.worker_response(request_id, kind, url=url)
                self.assertIsNone(self.observer._store.get('', request_id))
                self.assertEqual(len(child.extras), 1)
                self.assertEqual(len(child.response_extras), 1)
                observed = self.observer._store.snapshot(child)
                self.assertTrue(observed.context_complete)
                self.assertEqual(observed.context.headers, expected_headers)
                self.assertEqual(observed.context.url, url)
                self.assertEqual(observed.request_id, kind + ':' + request_id)
                self.assertEqual(observed.content_type, 'video/mp4')
                self.assertEqual(observed.content_length, 4000000)
                self.assertNotIn('origin', observed.context.headers)
                candidate = next(candidate for candidate in service.candidates.values() if candidate.url == url)
                self.assertEqual(candidate.media_type, 'Segment')
                self.assertTrue(candidate.metadata.get('fragment_only'))
                self.assertTrue(candidate.context_complete)
                self.assertEqual(candidate.request_context.headers, expected_headers)
                self.assertEqual(self.observer.network_diagnostics()['counts'].get('media_requests', 0), index + 1)

    def test_root_worker_extra_after_response_aligns_both_header_arrival_orders(self):
        self.page.answer('Network.enable')
        for response_first in (False, True):
            with self.subTest(response_first=response_first):
                session = 'late-response-first' if response_first else 'late-request-first'
                self.attach_worker(session)
                self.worker_request(session, session)
                self.worker_response(session, session)
                child = self.observer._store.get(session, session)
                self.assertFalse(self.observer._store.snapshot(child).context_complete)
                expected_headers = self.root_worker_extra(session, response_first=response_first)
                observed = self.observer._store.snapshot(child)
                self.assertTrue(observed.context_complete)
                self.assertEqual(observed.context.headers, expected_headers)
                self.assertEqual(observed.content_type, 'video/mp4')
                self.assertEqual(observed.content_length, 4000000)
                self.assertEqual(len(child.extras), 1)
                self.assertEqual(len(child.response_extras), 1)
                self.assertIsNone(self.observer._store.get('', session))

    def test_worker_extra_matches_request_id_even_when_media_urls_are_identical(self):
        self.page.answer('Network.enable')
        same_url = 'https://cdn.example/same-opaque?sign=same%2Bvalue'
        for session, request_id in (('one', 'request-one'), ('two', 'request-two')):
            self.attach_worker(session)
            self.worker_request(request_id, session, same_url)
            self.worker_response(request_id, session, url=same_url)
        expected_headers = self.root_worker_extra('request-one')
        one = self.observer._store.snapshot(self.observer._store.get('one', 'request-one'))
        two = self.observer._store.snapshot(self.observer._store.get('two', 'request-two'))
        self.assertEqual(one.context.url, two.context.url)
        self.assertEqual(one.context.headers, expected_headers)
        self.assertTrue(one.context_complete)
        self.assertFalse(two.context_complete)
        self.assertEqual(two.context.cookies, '')
        self.assertNotIn('authorization', two.context.headers)
        self.root_worker_extra('no-owner')
        self.assertIsNotNone(self.observer._store.get('', 'no-owner'))
        self.assertFalse(self.observer._store.get('two', 'request-two').extras)

    def test_worker_extra_with_same_id_in_multiple_owners_stays_unassigned(self):
        self.page.answer('Network.enable')
        for session in ('one', 'two'):
            self.attach_worker(session)
            self.worker_request('ambiguous', session, 'https://cdn.example/' + session)
        self.root_worker_extra('ambiguous')
        for session in ('one', 'two'):
            self.worker_response('ambiguous', session, url='https://cdn.example/' + session)
            child = self.observer._store.get(session, 'ambiguous')
            self.assertFalse(child.extras)
            observed = self.observer._store.snapshot(child)
            self.assertFalse(observed.context_complete)
            self.assertEqual(observed.context.cookies, '')
            self.assertNotIn('authorization', observed.context.headers)
        root = self.observer._store.get('', 'ambiguous')
        self.assertFalse(root.hops)
        self.assertEqual(len(root.extras), 1)
        self.assertEqual(len(root.response_extras), 1)
        self.assertEqual(self.observer.network_diagnostics()['counts'].get('worker_extra_info_aligned', 0), 0)

    def test_root_actual_request_keeps_its_extra_without_supplying_worker_credentials(self):
        self.page.answer('Network.enable')
        self.attach_worker()
        self.worker_request('root-owned', '', 'https://page.example/real-root-request')
        self.worker_response('root-owned', '', url='https://page.example/real-root-request')
        self.worker_request('root-owned')
        self.worker_response('root-owned')
        expected_headers = self.root_worker_extra('root-owned')
        root = self.observer._store.get('', 'root-owned')
        self.assertEqual(len(root.hops), 1)
        self.assertEqual(self.observer._store.snapshot(root).context.headers, expected_headers)
        worker = self.observer._store.snapshot(self.observer._store.get('worker', 'root-owned'))
        self.assertFalse(worker.context_complete)
        self.assertEqual(worker.context.cookies, '')
        self.assertNotIn('authorization', worker.context.headers)
        self.assertEqual(self.observer.network_diagnostics()['counts'].get('worker_extra_info_aligned', 0), 0)

    def test_iframe_detached_or_unknown_worker_cannot_receive_root_extra(self):
        self.page.answer('Network.enable')
        self.attach_worker('iframe', 'iframe')
        self.attach_worker('detached')
        self.worker_request('iframe-request', 'iframe')
        self.worker_response('iframe-request', 'iframe')
        self.worker_request('detached-request', 'detached')
        self.worker_response('detached-request', 'detached')
        self.event('Target.detachedFromTarget', {'sessionId': 'detached'})
        self.worker_request('unknown-request', 'unknown')
        self.worker_response('unknown-request', 'unknown')
        for session in ('iframe', 'detached', 'unknown'):
            with self.subTest(session=session):
                request_id = session + '-request'
                self.root_worker_extra(request_id)
                self.assertIsNotNone(self.observer._store.get('', request_id))
                child = self.observer._store.get(session, request_id)
                if child is not None:
                    self.assertFalse(child.extras)
                    self.assertFalse(self.observer._store.snapshot(child).context_complete)
                    self.assertEqual(self.observer._store.snapshot(child).context.cookies, '')
                else:
                    self.assertEqual(session, 'unknown')
        self.assertEqual(self.observer.network_diagnostics()['counts'].get('worker_extra_info_aligned', 0), 0)

    def test_worker_without_explicit_response_extra_evidence_keeps_root_headers_unassigned(self):
        self.page.answer('Network.enable')
        for has_extra in (False, None):
            with self.subTest(has_extra=has_extra):
                session = 'false-extra' if has_extra is False else 'missing-extra'
                self.attach_worker(session)
                self.root_worker_extra(session)
                self.worker_request(session, session)
                self.worker_response(session, session, has_extra=has_extra)
                child = self.observer._store.get(session, session)
                self.assertFalse(child.extras)
                self.assertFalse(child.response_extras)
                self.assertIsNotNone(self.observer._store.get('', session))
                observed = self.observer._store.snapshot(child)
                self.assertEqual(observed.context.cookies, '')
                self.assertNotIn('authorization', observed.context.headers)
        self.assertEqual(self.observer.network_diagnostics()['counts'].get('worker_extra_info_aligned', 0), 0)

    def test_worker_extra_credentials_stay_in_context_without_raw_user_facing_logs(self):
        self.page.answer('Network.enable')
        self.attach_worker()
        observations, notices = [], []
        self.observer.observation.connect(observations.append)
        self.observer.status.connect(notices.append)
        self.observer.error.connect(notices.append)
        captured = io.StringIO()
        with redirect_stdout(captured), redirect_stderr(captured):
            self.worker_request('private-extra')
            self.worker_response('private-extra')
            expected_headers = self.root_worker_extra('private-extra')
        observed = observations[-1]
        self.assertEqual(observed.context.headers, expected_headers)
        self.assertTrue(observed.context_complete)
        safe_output = captured.getvalue() + '\n'.join(notices) + repr(observed) + json.dumps(self.observer.network_diagnostics())
        for secret in ('worker-unit-secret', 'worker-unit-authorization', 'a%2Bb'):
            self.assertNotIn(secret, safe_output)

    def test_child_and_retired_page_events_do_not_open_current_document_gate(self):
        document_ready = []
        self.observer.document_ready.connect(lambda: document_ready.append(True))
        old = self.page
        current = FakePage()
        current.page_id = 'new-document'
        self.observer.set_page(current)
        self.observer.start()
        self.app.processEvents()
        current.answer('Network.enable')
        old.cdpMessage.emit({'method': 'Page.domContentEventFired', 'params': {}})
        current.cdpMessage.emit({'method': 'Target.attachedToTarget', 'params': {
            'sessionId': 'child', 'targetInfo': {'type': 'iframe'}}})
        current.cdpMessage.emit({'method': 'Page.domContentEventFired', 'sessionId': 'child', 'params': {}})
        self.assertEqual(document_ready, [])
        current.cdpMessage.emit({'method': 'Page.domContentEventFired', 'params': {}})
        self.assertEqual(document_ready, [True])

    def test_real_wire_headers_use_unchanged_store_and_no_url_reconstruction(self):
        seen = []
        self.observer.observation.connect(seen.append)
        self.page.answer('Network.enable')
        url = 'https://cdn.example/no-suffix?sign=a%2Bb&token=one&token=two'
        self.event('Network.requestWillBeSent', {'requestId': 'r1', 'wallTime': time.time(), 'type': 'Fetch',
            'request': {'url': url, 'method': 'GET', 'headers': {'Referer': 'https://page.example/watch'}}})
        self.event('Network.requestWillBeSentExtraInfo', {'requestId': 'r1', 'headers': {
            'Referer': 'https://page.example/watch', 'User-Agent': 'Edge-native', 'Range': 'bytes=0-',
            'Cookie': 'synthetic=local-only', 'Authorization': 'Bearer local-only'}})
        self.event('Network.responseReceived', {'requestId': 'r1', 'type': 'Fetch', 'hasExtraInfo': True,
            'response': {'url': url, 'status': 200, 'mimeType': 'application/vnd.apple.mpegurl', 'headers': {}}})
        self.assertTrue(seen)
        self.assertEqual(seen[-1].context.url, url)
        self.assertTrue(seen[-1].context_complete)
        self.assertEqual(seen[-1].context.headers['authorization'], 'Bearer local-only')
        self.assertNotIn('origin', seen[-1].context.headers)

    def test_iframe_session_command_and_events_are_preserved(self):
        self.page.answer('Network.enable')
        self.event('Target.attachedToTarget', {'sessionId': 'child', 'targetInfo': {'type': 'iframe'}})
        commands = [item[0] for item in self.page.commands if item[0].get('sessionId') == 'child']
        self.assertIn('Network.enable', [item['method'] for item in commands])
        self.event('Runtime.executionContextCreated', {'context': {'id': 17, 'auxData': {
            'isDefault': True, 'frameId': 'nested-frame'}}}, 'child')
        self.assertEqual(self.observer._contexts[('child', 17)], 'nested-frame')
        self.event('Runtime.executionContextCreated', {'context': {'id': 18, 'auxData': {
            'isDefault': True, 'frameId': 'foreign-frame'}}}, 'foreign')
        self.assertNotIn(('foreign', 18), self.observer._contexts)

    def test_old_pipe_reply_and_events_after_replacing_page_are_ignored(self):
        old = self.page
        seen = []
        self.observer.ready.connect(lambda: seen.append(True))
        new = FakePage()
        new.page_id = 'new-page'
        self.observer.set_page(new)
        self.observer.start()
        self.app.processEvents()
        old.answer('Network.enable')
        old.cdpMessage.emit({'method': 'Runtime.executionContextCreated', 'params': {
            'context': {'id': 99, 'auxData': {'isDefault': True, 'frameId': 'old'}}}})
        self.assertFalse(seen)
        self.assertNotIn(('', 99), self.observer._contexts)
        new.answer('Network.enable')
        self.assertEqual(seen, [True])

    def test_stopping_does_not_close_retained_native_profile(self):
        self.page.answer('Network.enable')
        socket = self.observer._socket
        self.observer.stop()
        self.assertFalse(socket._open)
        self.assertTrue(self.page.is_ready)
        self.assertFalse(self.observer._commands)
        self.assertFalse(self.observer._command_timer.isActive())

    def test_host_error_stops_current_observer_with_static_message(self):
        errors = []
        self.observer.error.connect(errors.append)
        self.page.backendError.emit('WebView2 宿主已退出。')
        self.assertEqual(errors, ['WebView2 宿主已退出。'])
        self.assertFalse(self.observer._active)


class NativePipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = _PipeHost()

    def tearDown(self):
        self.host.shutdown()
        self.host._cleanup()

    def test_ipc_budget_covers_base64_and_double_json_escaping_with_absolute_bound(self):
        mib = 1024 * 1024
        self.assertEqual(self.host.max_line_bytes, 129 * mib)
        self.assertGreater(response_line_limit(), 16 * mib * 4 // 3)
        self.assertEqual(response_line_limit(128 * mib), MAX_LINE_BYTES)
        self.assertEqual(response_line_limit(1024 * mib), MAX_LINE_BYTES)
        self.assertEqual(MAX_REQUEST_LINE_BYTES, 8 * mib)
        # 用实际最坏 ASCII 控制字符确认两层 JSON 编码没有超出预算；
        # 缩小样本以免测试为验证上限本身分配百兆内容。
        body = '\x00' * 1024
        encoded = json.dumps({'result': json.dumps({'body': body})}, ensure_ascii=True).encode()
        self.assertLess(len(encoded), len(body) * 8 + mib)

    def test_shared_profile_increases_receive_budget_without_shrinking_live_page_budget(self):
        mib = 1024 * 1024
        self.host.set_capture_body_limit(48 * mib)
        self.assertEqual(self.host.max_line_bytes, response_line_limit(48 * mib))
        self.host.set_capture_body_limit(16 * mib)
        self.assertEqual(self.host.max_line_bytes, response_line_limit(48 * mib))

    def test_oversized_pipe_frame_is_rejected_before_json_dispatch(self):
        self.host.max_line_bytes = 32
        with patch.object(self.host.process, 'readAllStandardOutput', return_value=b'x' * 33), \
                patch.object(self.host, '_protocol_failed') as failed:
            self.host._read()
            failed.assert_called_once()

    def test_complete_pipe_frame_uses_configured_receive_budget(self):
        frame = json.dumps({'event': 'cdp', 'value': 'x' * 64}).encode() + b'\n'
        self.host.max_line_bytes = len(frame)
        with patch.object(self.host.process, 'readAllStandardOutput', return_value=frame), \
                patch.object(self.host, '_dispatch') as dispatch:
            self.host._read()
            self.assertEqual(dispatch.call_args.args[0]['value'], 'x' * 64)

    def test_correlated_reply_is_delivered_once_and_unknown_reply_ignored(self):
        values = []
        self.host._pending[12] = (time.monotonic() + 5, lambda result, ok: values.append((result, ok)))
        self.host._dispatch({'id': 99, 'ok': True, 'result': 'unrelated'})
        self.host._dispatch({'id': 12, 'ok': True, 'result': {'value': True}})
        self.host._dispatch({'id': 12, 'ok': True, 'result': 'duplicate'})
        self.assertEqual(values, [({'value': True}, True)])

    def test_request_timeout_releases_callback_without_raw_native_error(self):
        values = []
        self.host._pending[2] = (time.monotonic() - 1, lambda *args: values.append(args))
        self.host._expire()
        self.assertEqual(values, [(None, False)])
        self.assertFalse(self.host._pending)

    def test_unsolicited_events_do_not_enter_command_callback(self):
        events = []
        self.host.event.connect(events.append)
        self.host._dispatch({'event': 'cdp', 'page': 'one', 'message': {'method': 'Network.responseReceived'}})
        self.assertEqual(len(events), 1)
        self.host._closing = True
        self.host._dispatch({'event': 'cdp', 'page': 'one', 'message': {}})
        self.assertEqual(len(events), 1)
        self.host._closing = False

    def test_runtime_directory_cleanup_cannot_target_a_parent_or_user_profile(self):
        original = self.host.user_data_folder
        self.host.user_data_folder = self.host._session_root
        with patch('ui.webview2_browser.shutil.rmtree') as remove:
            self.host._cleanup()
        remove.assert_not_called()
        self.host.user_data_folder = original

    def test_explicit_qt_backend_and_retained_qt_profile_are_supported(self):
        self.assertEqual(select_browser_backend(requested='qtwebengine'), 'qtwebengine')
        self.assertEqual(select_browser_backend(QObject(), requested='webview2'), 'qtwebengine')

    def test_invalid_backend_is_rejected(self):
        with self.assertRaises(ValueError):
            select_browser_backend(requested='external-browser')


if __name__ == '__main__':
    unittest.main()
