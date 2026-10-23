import base64
import json
import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtNetwork import QAbstractSocket
from PySide6.QtWidgets import QApplication, QWidget

from core import browser_runtime
from core.browser_observer import (BrowserNetworkObserver, MAX_BODY_BYTES, MAX_HEADER_BYTES,
                                   MAX_POST_BYTES, MAX_RECORDS, MAX_TOTAL_BODY_BYTES,
                                   _NetworkStore, _headers)
from core.dom_scanner import DOM_OBSERVER_SCRIPT, DOM_SNAPSHOT_SCRIPT


URL = 'https://media.example/opaque?sign=test-sign&t=123&token=test-token'


def request(request_id='r1', url=URL, method='GET', headers=None, **extra):
    value = {'url': url, 'method': method, 'headers': headers or {'Referer': 'https://page.example/watch', 'User-Agent': 'Browser UA'}}
    value.update(extra)
    return {'requestId': request_id, 'request': value, 'frameId': 'main', 'type': 'Fetch', 'wallTime': time.time()}


def response(request_id='r1', url=URL, mime='application/vnd.apple.mpegurl', status=200, has_extra=True, **extra):
    value = {'url': url, 'status': status, 'mimeType': mime, 'headers': {'Content-Length': '123'}}
    value.update(extra)
    return {'requestId': request_id, 'response': value, 'type': 'Fetch', 'hasExtraInfo': has_extra}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = _NetworkStore()

    def consume(self, kind, params, session=''):
        return self.store.consume('Network.' + kind, params, session)

    def start(self, **kwargs):
        self.consume('requestWillBeSent', request(**kwargs))

    def wire(self, request_id='r1', headers=None, session=''):
        return self.consume('requestWillBeSentExtraInfo', {'requestId': request_id, 'headers': headers or {
            'User-Agent': 'Browser UA', 'Referer': 'https://page.example/watch', 'Cookie': 'session=test-cookie',
            'Authorization': 'Bearer test-authorization', ':authority': 'media.example'}}, session)

    def final(self, request_id='r1', session=''):
        return self.store.snapshot(self.store.get(session, request_id))

    def test_wire_headers_include_cookie_authorization_and_ignore_pseudo_headers(self):
        self.start()
        self.wire()
        self.consume('responseReceived', response())
        observation = self.final()
        self.assertTrue(observation.context_complete)
        self.assertEqual(observation.context.cookies, 'session=test-cookie')
        self.assertEqual(observation.context.headers['authorization'], 'Bearer test-authorization')
        self.assertNotIn(':authority', observation.context.headers)

    def test_missing_origin_cross_site_remains_absent(self):
        self.start()
        self.wire(headers={'Referer': 'https://page.example/watch', 'User-Agent': 'UA', 'Range': 'bytes=0-'})
        self.consume('responseReceived', response())
        context = self.final().context
        self.assertEqual(context.origin, '')
        self.assertTrue(context.inherit_range)
        self.assertEqual(context.derive('enc.key').headers['range'], 'bytes=0-')
        self.assertNotIn('origin', context.derive('abc000.png').headers)

    def test_partial_seek_range_is_recorded_but_not_unconditionally_inherited(self):
        self.start()
        self.wire(headers={'Range': 'bytes=1048576-2097151', 'Referer': 'https://page.example/watch'})
        self.consume('responseReceived', response())
        context = self.final().context
        self.assertEqual(context.headers['range'], 'bytes=1048576-2097151')
        self.assertFalse(context.inherit_range)
        self.assertNotIn('range', context.derive('key').headers)

    def test_explicit_origin_is_kept(self):
        self.start()
        self.wire(headers={'Origin': 'https://page.example', 'Range': 'bytes=0-'})
        self.consume('responseReceived', response())
        self.assertEqual(self.final().context.origin, 'https://page.example')

    def test_browser_metadata_is_saved_without_default_inheritance(self):
        self.start()
        self.wire(headers={'Sec-Fetch-Dest': 'video', 'Sec-Fetch-Site': 'cross-site', 'Sec-CH-UA': 'Browser'})
        self.consume('responseReceived', response())
        context = self.final().context
        self.assertEqual(context.browser_metadata['sec-fetch-site'], 'cross-site')
        self.assertNotIn('sec-fetch-site', context.derive('child').headers)

    def test_query_and_escaped_signature_remain_exact(self):
        signed = 'https://media.example/index?sign=abc%2Fdef%2Bghi&token=A%3DB&t=100&expires=&signature=x+y'
        self.start(url=signed)
        self.wire()
        self.consume('responseReceived', response(url=signed))
        self.assertEqual(self.final().context.url, signed)
        self.assertEqual(self.final().context.original_url, signed)
        self.assertEqual(self.final().context.final_url, signed)

    def test_extra_info_before_request_is_reconciled(self):
        self.wire()
        self.start()
        self.consume('responseReceived', response())
        self.assertEqual(self.final().context.cookies, 'session=test-cookie')
        self.assertTrue(self.final().context_complete)

    def test_extra_info_after_response_updates_incomplete_context(self):
        self.start()
        self.consume('responseReceived', response())
        self.assertFalse(self.final().context_complete)
        updated = self.wire()[0]
        self.assertTrue(updated.context_complete)
        self.assertEqual(updated.context.cookies, 'session=test-cookie')

    def test_media_content_type_supplies_empty_or_generic_cdp_mime(self):
        for cdp_mime in ('', 'application/octet-stream', 'binary/octet-stream', 'application/binary'):
            for wire_mime in ('video/mp4', 'audio/mpeg', 'application/vnd.apple.mpegurl', 'application/dash+xml'):
                with self.subTest(cdp_mime=cdp_mime, wire_mime=wire_mime):
                    self.store.clear()
                    self.start()
                    self.consume('responseReceived', response(mime=cdp_mime, has_extra=False,
                        headers={'Content-Type': wire_mime.upper() + '; charset=UTF-8', 'Content-Length': '456'}))
                    observed = self.final()
                    self.assertEqual(observed.content_type, wire_mime)
                    self.assertEqual(observed.content_length, 456)
                    self.assertEqual(observed.context.url, URL)
                    self.assertEqual(observed.resource_type, 'Fetch')

    def test_response_extra_before_request_before_response_or_late_updates_media_details(self):
        extra = {'requestId': 'r1', 'statusCode': 206, 'headers': {
            'Content-Type': 'video/mp4; codecs="avc1.42E01E"', 'Content-Length': '456',
            'Content-Range': 'bytes 0-455/456'}}
        for arrival in ('before_request', 'before_response', 'after_response'):
            with self.subTest(arrival=arrival):
                self.store.clear()
                if arrival == 'before_request':
                    self.consume('responseReceivedExtraInfo', extra)
                self.start()
                if arrival == 'before_response':
                    self.consume('responseReceivedExtraInfo', extra)
                self.consume('responseReceived', response(mime='application/octet-stream', status=206))
                if arrival == 'after_response':
                    self.assertEqual(self.final().content_type, 'application/octet-stream')
                    self.assertEqual(self.final().content_length, 123)
                    self.consume('responseReceivedExtraInfo', extra)
                observed = self.final()
                self.assertEqual(observed.content_type, 'video/mp4')
                self.assertEqual(observed.content_length, 456)
                self.assertEqual(observed.response_headers['content-range'], 'bytes 0-455/456')
                self.assertEqual(observed.context.url, URL)
                self.assertNotIn('content-type', observed.context.headers)

    def test_generic_wire_headers_do_not_replace_browser_identified_media_mime(self):
        for identified in ('video/mp4', 'audio/mpeg', 'application/vnd.apple.mpegurl', 'application/dash+xml'):
            with self.subTest(identified=identified):
                self.store.clear()
                self.start()
                self.consume('responseReceived', response(mime=identified,
                    headers={'Content-Type': 'application/octet-stream', 'Content-Length': '123'}))
                self.assertEqual(self.final().content_type, identified)
                self.consume('responseReceivedExtraInfo', {'requestId': 'r1', 'statusCode': 200,
                    'headers': {'Content-Type': 'binary/octet-stream', 'Content-Length': '789'}})
                self.assertEqual(self.final().content_type, identified)
                self.assertEqual(self.final().content_length, 789)

    def test_normal_noncache_response_without_extra_info_can_be_reproduced(self):
        self.start(headers={'User-Agent': 'UA', 'Authorization': 'Bearer explicit'})
        self.consume('responseReceived', response(has_extra=False))
        self.assertTrue(self.final().context_complete)
        self.assertEqual(self.final().context.headers['authorization'], 'Bearer explicit')

    def test_cache_without_wire_does_not_invent_cookie_or_completeness(self):
        self.start()
        self.consume('responseReceived', response(has_extra=False, fromDiskCache=True))
        result = self.final()
        self.assertTrue(result.from_cache)
        self.assertFalse(result.context_complete)
        self.assertEqual(result.context.cookies, '')

    def test_cached_response_with_actual_wire_is_complete(self):
        self.start()
        self.wire()
        self.consume('responseReceived', response(fromDiskCache=True))
        self.assertTrue(self.final().from_cache)
        self.assertTrue(self.final().context_complete)

    def test_redirect_keeps_original_final_chain_and_final_actual_headers(self):
        original = 'https://page.example/media?token=first'
        final = 'https://cdn.example/manifest?sign=second'
        self.start(url=original)
        self.wire(headers={'Authorization': 'Bearer original', 'Origin': 'https://page.example'})
        redirect = request(url=final)
        redirect['redirectResponse'] = response(url=original, status=302, mime='text/html')['response']
        redirect['redirectResponse']['headers']['Location'] = final
        redirect['redirectHasExtraInfo'] = True
        self.consume('requestWillBeSent', redirect)
        self.wire(headers={'Authorization': 'Bearer final', 'Referer': 'https://page.example/watch', 'Cookie': 'session=final'})
        self.consume('responseReceived', response(url=final))
        result = self.final()
        self.assertEqual(result.context.original_url, original)
        self.assertEqual(result.context.final_url, final)
        self.assertEqual(result.context.headers['authorization'], 'Bearer final')
        self.assertNotIn('origin', result.context.headers)
        self.assertEqual(result.redirects, [{'from': original, 'to': final, 'status': 302, 'location': final}])
        self.assertTrue(result.context_complete)

    def test_redirect_hop_without_extra_does_not_consume_next_hops_cookie(self):
        final = 'https://cdn.example/second'
        self.start()
        # 第二 hop 的 ExtraInfo 先到；尚未知第一 hop 不发送 ExtraInfo。
        self.wire(headers={'Cookie': 'session=second', 'User-Agent': 'second UA'})
        redirect = request(url=final)
        redirect['redirectResponse'] = response(status=302)['response']
        redirect['redirectHasExtraInfo'] = False
        self.consume('requestWillBeSent', redirect)
        self.consume('responseReceived', response(url=final))
        self.assertEqual(self.final().context.cookies, 'session=second')
        self.assertTrue(self.final().context_complete)

    def test_extra_info_from_both_redirect_hops_before_normal_redirect(self):
        final = 'https://cdn.example/final'
        self.wire(headers={'Cookie': 'session=original'})
        self.wire(headers={'Cookie': 'session=final'})
        self.start()
        redirect = request(url=final)
        redirect['redirectResponse'] = response(status=307)['response']
        redirect['redirectHasExtraInfo'] = True
        self.consume('requestWillBeSent', redirect)
        self.consume('responseReceived', response(url=final))
        self.assertEqual(self.final().context.cookies, 'session=final')
        self.assertEqual(self.final().redirects[0]['status'], 307)

    def test_redirect_final_method_and_body_follow_browser_not_assumptions(self):
        self.start(method='POST', postData='payload=one')
        self.wire(headers={'Content-Type': 'application/x-www-form-urlencoded'})
        final = 'https://cdn.example/final'
        redirect = request(url=final, method='GET')
        redirect['redirectResponse'] = response(status=302)['response']
        redirect['redirectHasExtraInfo'] = True
        self.consume('requestWillBeSent', redirect)
        self.wire(headers={'User-Agent': 'UA'})
        self.consume('responseReceived', response(url=final))
        self.assertEqual(self.final().context.method, 'GET')
        self.assertIsNone(self.final().context.body)

    def test_same_request_id_in_different_iframe_sessions_is_isolated(self):
        self.consume('requestWillBeSent', request(url='https://one.example/media'), 'iframe-one')
        self.consume('requestWillBeSent', request(url='https://two.example/media'), 'iframe-two')
        self.wire(headers={'Cookie': 'one=secret'}, session='iframe-one')
        self.wire(headers={'Cookie': 'two=secret'}, session='iframe-two')
        self.consume('responseReceived', response(url='https://one.example/media'), 'iframe-one')
        self.consume('responseReceived', response(url='https://two.example/media'), 'iframe-two')
        self.assertEqual(self.final(session='iframe-one').context.cookies, 'one=secret')
        self.assertEqual(self.final(session='iframe-two').context.cookies, 'two=secret')
        self.assertNotEqual(self.final(session='iframe-one').request_id, self.final(session='iframe-two').request_id)

    def test_direct_206_is_success_and_body_probe_is_eligible(self):
        self.start()
        self.wire()
        self.consume('responseReceived', response(mime='video/mp4', status=206))
        self.consume('loadingFinished', {'requestId': 'r1', 'encodedDataLength': 100})
        record = self.store.get('', 'r1')
        self.assertEqual(self.final().status, 206)
        self.assertTrue(self.store.wants_body(record))

    def test_http403_observation_remains_http403_and_has_no_drm_flag(self):
        self.start()
        self.wire()
        self.consume('responseReceived', response(status=403, mime='text/html'))
        result = self.final()
        self.assertEqual(result.status, 403)
        self.assertFalse(hasattr(result, 'drm'))

    def test_response_extra_before_response_can_supply_http403(self):
        self.start()
        self.consume('responseReceivedExtraInfo', {'requestId': 'r1', 'statusCode': 403, 'headers': {'Content-Type': 'text/plain'}})
        self.assertEqual(self.final().status, 403)
        self.assertEqual(self.final().content_type, 'text/plain')

    def test_post_json_body_preserved_as_bytes(self):
        self.start(method='POST', postData='{"title":"中文"}', headers={'Content-Type': 'application/json'})
        self.wire(headers={'Content-Type': 'application/json', 'Authorization': 'Bearer test'})
        self.consume('responseReceived', response())
        self.assertEqual(self.final().context.body, '{"title":"中文"}'.encode())
        self.assertTrue(self.final().context_complete)

    def test_post_raw_bytes_entries_are_preserved(self):
        raw = b'\x00\xffbinary\x01'
        self.start(method='POST', postDataEntries=[{'bytes': base64.b64encode(raw).decode()}])
        self.wire()
        self.consume('responseReceived', response())
        self.assertEqual(self.final().context.body, raw)

    def test_missing_post_body_is_incomplete_until_retrieved(self):
        self.start(method='POST', hasPostData=True)
        self.wire()
        self.consume('responseReceived', response())
        self.assertFalse(self.final().context_complete)
        record = self.store.get('', 'r1')
        updated = self.store.set_post_body(record, record.hops[-1], {'postData': 'exact=payload'})
        self.assertTrue(updated.context_complete)
        self.assertEqual(updated.context.body, b'exact=payload')

    def test_multipart_missing_files_is_never_claimed_complete(self):
        self.start(method='POST', hasPostData=True, postData='text without file', headers={'Content-Type': 'multipart/form-data; boundary=x'})
        self.wire()
        self.consume('responseReceived', response())
        self.assertFalse(self.final().context_complete)
        self.assertIsNone(self.final().context.body)

    def test_oversize_post_body_is_not_retained_or_claimed_complete(self):
        self.start(method='POST', postData='x' * (MAX_POST_BYTES + 1))
        self.wire()
        self.consume('responseReceived', response())
        self.assertIsNone(self.final().context.body)
        self.assertFalse(self.final().context_complete)

    def test_body_bytes_base64_decoded_without_suffix_classification(self):
        self.start(url='https://media.example/abc000.png')
        self.wire()
        self.consume('responseReceived', response(url='https://media.example/abc000.png', mime='video/mp2t'))
        record = self.store.get('', 'r1')
        result = self.store.set_body(record, record.hops[-1], {'body': base64.b64encode(b'\x47ts-payload').decode(), 'base64Encoded': True})
        self.assertEqual(result.body, b'\x47ts-payload')
        self.assertTrue(result.body_complete)
        self.assertEqual(result.content_type, 'video/mp2t')

    def test_single_body_and_cumulative_budget_are_enforced(self):
        self.start()
        record = self.store.get('', 'r1')
        self.assertIsNone(self.store.set_body(record, record.hops[-1], {'body': 'x' * (MAX_BODY_BYTES + 1)}))
        self.store.total_body_read = MAX_TOTAL_BODY_BYTES - 2
        self.assertIsNone(self.store.set_body(record, record.hops[-1], {'body': 'abc'}))
        self.assertIsNone(record.hops[-1].body)

    def test_body_callback_for_old_redirect_hop_is_ignored(self):
        self.start()
        record = self.store.get('', 'r1')
        hop = record.hops[-1]
        redirect = request(url='https://cdn.example/new')
        redirect['redirectResponse'] = response(status=302)['response']
        self.consume('requestWillBeSent', redirect)
        self.assertIsNone(self.store.set_body(record, hop, {'body': '#EXTM3U'}))
        self.assertIsNone(record.hops[-1].body)

    def test_body_callback_for_evicted_or_cleared_record_is_ignored(self):
        self.start()
        record = self.store.get('', 'r1')
        self.store.clear()
        self.assertIsNone(self.store.set_body(record, record.hops[-1], {'body': '#EXTM3U'}))

    def test_large_video_is_not_downloaded_for_sniffing(self):
        self.start()
        self.consume('responseReceived', response(mime='video/mp4', headers={'Content-Length': str(MAX_BODY_BYTES * 4)}))
        self.consume('loadingFinished', {'requestId': 'r1', 'encodedDataLength': MAX_BODY_BYTES * 4})
        self.assertFalse(self.store.wants_body(self.store.get('', 'r1')))

    def test_unknown_size_stream_is_not_downloaded_for_sniffing(self):
        self.start()
        self.consume('responseReceived', response(mime='video/mp4', headers={}))
        record = self.store.get('', 'r1')
        record.hops[-1].finished = True
        self.assertFalse(self.store.wants_body(record))

    def test_license_url_and_unknown_post_octet_stream_are_not_read(self):
        for url, method in (('https://media.example/license?token=x', 'POST'), ('https://media.example/opaque', 'POST')):
            with self.subTest(url=url):
                self.store.clear()
                self.start(url=url, method=method)
                self.consume('responseReceived', response(url=url, mime='application/octet-stream'))
                self.consume('loadingFinished', {'requestId': 'r1', 'encodedDataLength': 100})
                self.assertFalse(self.store.wants_body(self.store.get('', 'r1')))

    def test_manifest_with_octet_stream_and_no_extension_is_read(self):
        self.start()
        self.consume('responseReceived', response(mime='application/octet-stream'))
        self.consume('loadingFinished', {'requestId': 'r1', 'encodedDataLength': 100})
        self.assertTrue(self.store.wants_body(self.store.get('', 'r1')))

    def test_filename_extension_only_enables_sniffing_never_sets_media_type(self):
        self.start(url='https://media.example/opaque.m3u8')
        self.consume('responseReceived', response(url='https://media.example/opaque.m3u8', mime='text/plain'))
        result = self.final()
        self.assertEqual(result.content_type, 'text/plain')
        self.assertFalse(hasattr(result, 'media_type'))

    def test_record_limit_evicts_oldest_and_orphan_extra_is_bounded(self):
        for index in range(MAX_RECORDS + 3):
            self.consume('requestWillBeSentExtraInfo', {'requestId': str(index), 'headers': {'Cookie': 'small'}})
        self.assertEqual(len(self.store.records), MAX_RECORDS)
        self.assertIsNone(self.store.get('', '0'))

    def test_invalid_headers_and_oversize_headers_do_not_claim_complete(self):
        self.assertEqual(_headers({':path': '/index', 'Cookie': 'ok'})[0], {'cookie': 'ok'})
        headers, valid = _headers({'Cookie': 'x' * (MAX_HEADER_BYTES + 1)})
        self.assertEqual(headers, {})
        self.assertFalse(valid)
        self.start()
        self.wire(headers={'Cookie': 'bad\nvalue', 'User-Agent': 'UA'})
        self.consume('responseReceived', response())
        self.assertFalse(self.final().context_complete)

    def test_non_http_sources_and_blob_are_not_network_download_contexts(self):
        for url in ('blob:https://page.example/123', 'file:///private', 'data:video/mp4;base64,aA=='):
            self.assertEqual(self.consume('requestWillBeSent', request(url=url)), [])
        self.assertEqual(len(self.store.records), 0)

    def test_initiator_capture_is_bounded_and_never_contains_post_data(self):
        value = request(method='POST', postData='private=not-in-initiator')
        value['initiator'] = {'type': 'script', 'stack': {'callFrames': [{'url': 'https://page.example/player.js', 'functionName': 'loadMedia'}]}}
        self.consume('requestWillBeSent', value)
        self.assertIn('player.js', self.final().initiator)
        self.assertNotIn('not-in-initiator', self.final().initiator)

    def test_observation_repr_does_not_emit_sensitive_headers_body_or_signed_query(self):
        self.start(method='POST', postData='private-post')
        self.wire()
        self.consume('responseReceived', response())
        value = repr(self.final())
        for sensitive in ('test-cookie', 'test-authorization', 'private-post', 'test-sign', 'test-token'):
            self.assertNotIn(sensitive, value)


class FakePage:
    def devToolsId(self):
        return 'own-page'


class FakeSocket(QObject):
    connected = Signal()
    disconnected = Signal()
    errorOccurred = Signal(object)
    textMessageReceived = Signal(str)

    def __init__(self, origin='', parent=None):
        super().__init__(parent)
        self.origin = origin
        self.messages = []
        self.aborted = False
        self.open_url = ''

    def state(self):
        return QAbstractSocket.SocketState.ConnectedState

    def setProxy(self, value):
        pass

    def setMaxAllowedIncomingMessageSize(self, value):
        self.message_limit = value

    def setMaxAllowedIncomingFrameSize(self, value):
        pass

    def sendTextMessage(self, value):
        self.messages.append(json.loads(value))

    def open(self, url):
        self.open_url = url.toString()

    def abort(self):
        self.aborted = True


class FakeReply:
    def __init__(self, payload):
        self.payload = payload
        self.deleted = False
        self.aborted = False

    def readAll(self):
        payload, self.payload = self.payload, b''
        return payload

    def deleteLater(self):
        self.deleted = True

    def abort(self):
        self.aborted = True


class ObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.observer = BrowserNetworkObserver(FakePage())
        self.observer._active = True
        self.observer._port = 12345
        self.observer._page_id = 'own-page'
        self.observer._socket = FakeSocket(parent=self.observer)
        self.observations = []
        self.dom = []
        self.errors = []
        self.observer.observation.connect(self.observations.append)
        self.observer.dom.connect(self.dom.append)
        self.observer.error.connect(self.errors.append)

    def tearDown(self):
        self.observer.stop()
        self.observer.deleteLater()
        self.app.processEvents()

    def event(self, method, params, session=''):
        message = {'method': method, 'params': params}
        if session:
            message['sessionId'] = session
        self.observer._receive_text(json.dumps(message))

    def answer(self, command, result=None, error=False):
        self.observer._receive_text(json.dumps({'id': command['id'], 'error' if error else 'result': result or {}}))

    def test_ready_only_after_network_enable_success(self):
        ready = []
        self.observer.ready.connect(lambda: ready.append(True))
        self.observer._connected()
        commands = self.observer._socket.messages
        self.assertFalse(ready)
        self.answer(next(command for command in commands if command['method'] == 'Runtime.enable'))
        self.assertFalse(ready)
        self.answer(next(command for command in commands if command['method'] == 'Network.enable'))
        self.assertEqual(ready, [True])
        self.assertTrue(self.observer._enabled)

    def test_network_enable_error_is_fixed_safe_message_and_stops(self):
        self.observer._connected()
        command = next(command for command in self.observer._socket.messages if command['method'] == 'Network.enable')
        self.answer(command, {'message': 'Cookie: TOP-SECRET'}, error=True)
        self.assertFalse(self.observer._active)
        self.assertNotIn('TOP-SECRET', ''.join(self.errors))
        self.assertTrue(self.errors)

    def test_old_command_response_after_reset_is_ignored(self):
        hits = []
        command_id = self.observer._send('Test.command', callback=lambda result, success: hits.append(result))
        self.observer._enabled = True
        self.observer.reset()
        self.observer._receive_text(json.dumps({'id': command_id, 'result': {'secret': 'old'}}))
        self.assertEqual(hits, [])

    def test_reset_ignores_late_old_requests_and_bodies(self):
        self.event('Network.requestWillBeSent', request())
        self.observer._enabled = True
        self.observer.reset()
        self.event('Network.responseReceived', response())
        self.event('Network.requestWillBeSentExtraInfo', {'requestId': 'r1', 'headers': {'Cookie': 'old'}})
        self.assertEqual(len(self.observer._store.records), 0)

    def test_replacing_page_clears_old_target_ids_contexts_commands_and_socket(self):
        socket = self.observer._socket
        self.event('Network.requestWillBeSent', request())
        self.observer._contexts[('', 10)] = 'old-frame'
        self.observer._send('Runtime.evaluate', {'expression': 'old'})
        self.observer._ignored.append(('', 'reuse-after-target-change'))
        page = FakePage()
        self.observer.set_page(page)
        self.assertIs(self.observer.page, page)
        self.assertTrue(socket.aborted)
        self.assertFalse(self.observer._active)
        self.assertFalse(self.observer._store.records)
        self.assertFalse(self.observer._contexts)
        self.assertFalse(self.observer._commands)
        self.assertFalse(self.observer._ignored)

    def test_old_socket_messages_after_new_target_start_are_ignored(self):
        old = self.observer._socket
        self.observer.set_page(FakePage())
        current = FakeSocket(parent=self.observer)
        self.observer._socket, self.observer._active = current, True
        text = json.dumps({'method': 'Network.requestWillBeSent', 'params': request()})
        self.observer._socket_callback(old, self.observer._receive_text, text)
        self.observer._socket_callback(old, self.observer._socket_error, 'old error')
        self.observer._socket_callback(old, self.observer._disconnected)
        self.assertFalse(self.observations)
        self.assertFalse(self.errors)
        self.assertTrue(self.observer._active)
        self.observer._socket_callback(current, self.observer._receive_text, text)
        self.assertEqual(len(self.observations), 1)

    def test_stop_cancels_socket_and_all_timers_and_records(self):
        socket = self.observer._socket
        stopped = []
        self.observer.stopped.connect(lambda: stopped.append(True))
        self.event('Network.requestWillBeSent', request())
        self.observer._dom_timer.start()
        self.observer._retry_timer.start()
        self.observer.stop()
        self.assertTrue(socket.aborted)
        self.assertFalse(self.observer._dom_timer.isActive())
        self.assertFalse(self.observer._retry_timer.isActive())
        self.assertEqual(len(self.observer._store.records), 0)
        self.assertEqual(stopped, [True])
        self.observer.stop()
        self.assertEqual(stopped, [True])

    def test_stop_resumes_all_related_targets_and_disables_autoattach_before_cleanup(self):
        for kind in ('iframe', 'worker', 'shared_worker', 'service_worker'):
            self.event('Target.attachedToTarget', {'sessionId': kind, 'targetInfo': {'type': kind}})
        socket = self.observer._socket
        start = len(socket.messages)
        states = []
        original = socket.sendTextMessage

        def sending(payload):
            states.append((self.observer._active, socket.aborted, set(self.observer._sessions)))
            original(payload)

        with patch.object(socket, 'sendTextMessage', side_effect=sending):
            self.observer.stop()
        commands = socket.messages[start:]
        targets = {'iframe', 'worker', 'shared_worker', 'service_worker'}
        self.assertEqual(len(commands), 2 * len(targets) + 1)
        self.assertTrue(all(active and not aborted and sessions == targets for active, aborted, sessions in states))
        for session in targets:
            related = [command for command in commands if command.get('sessionId') == session]
            self.assertEqual([command['method'] for command in related],
                             ['Runtime.runIfWaitingForDebugger', 'Target.setAutoAttach'])
            self.assertEqual(related[1]['params'],
                             {'autoAttach': False, 'waitForDebuggerOnStart': False, 'flatten': True})
        root = [command for command in commands if not command.get('sessionId')]
        self.assertEqual(len(root), 1)
        self.assertEqual(root[0]['method'], 'Target.setAutoAttach')
        self.assertEqual(root[0]['params'], {'autoAttach': False, 'waitForDebuggerOnStart': False, 'flatten': True})
        self.assertTrue(socket.aborted)
        self.assertFalse(self.observer._active)
        self.assertFalse(self.observer._sessions)
        self.assertFalse(self.observer._commands)
        self.observer.stop()
        self.assertEqual(socket.messages[start:], commands)

    def test_autoattach_only_accepts_iframe_descendant(self):
        self.event('Target.attachedToTarget', {'sessionId': 'iframe-session', 'targetInfo': {'type': 'iframe'}})
        self.assertIn('iframe-session', self.observer._sessions)
        self.event('Target.attachedToTarget', {'sessionId': 'other-page', 'targetInfo': {'type': 'page'}})
        self.assertNotIn('other-page', self.observer._sessions)
        self.assertTrue(any(command['method'] == 'Target.detachFromTarget' for command in self.observer._socket.messages))

    def test_cache_disabled_before_network_enable_and_related_targets_paused_until_listening(self):
        self.observer._connected()
        commands = self.observer._socket.messages
        methods = [command['method'] for command in commands]
        self.assertLess(methods.index('Network.setCacheDisabled'), methods.index('Network.enable'))
        cache = next(command for command in commands if command['method'] == 'Network.setCacheDisabled')
        self.assertEqual(cache['params'], {'cacheDisabled': True})
        attach = next(command for command in commands if command['method'] == 'Target.setAutoAttach')
        self.assertTrue(attach['params']['waitForDebuggerOnStart'])
        self.assertTrue(attach['params']['flatten'])
        self.assertEqual({item['type'] for item in attach['params']['filter'] if not item['exclude']},
                         {'iframe', 'worker', 'shared_worker', 'service_worker'})
        self.assertEqual(attach['params']['filter'][-1], {'exclude': True})

    def test_iframe_and_worker_network_enable_success_or_failure_always_resume(self):
        for kind in ('iframe', 'worker', 'shared_worker', 'service_worker'):
            for failed in (False, True):
                with self.subTest(kind=kind, failed=failed):
                    session = kind + ('-failed' if failed else '-ok')
                    self.event('Target.attachedToTarget', {'sessionId': session, 'targetInfo': {'type': kind}})
                    commands = [command for command in self.observer._socket.messages if command.get('sessionId') == session]
                    self.assertFalse(any(command['method'] == 'Runtime.runIfWaitingForDebugger' for command in commands))
                    methods = [command['method'] for command in commands]
                    self.assertLess(methods.index('Network.setCacheDisabled'), methods.index('Network.enable'))
                    command = next(command for command in commands if command['method'] == 'Network.enable')
                    self.answer(command, {'message': 'not a user facing backend error'}, error=failed)
                    resumed = [command for command in self.observer._socket.messages
                               if command.get('sessionId') == session and command['method'] == 'Runtime.runIfWaitingForDebugger']
                    self.assertEqual(len(resumed), 1)
                    self.assertTrue(self.observer._active)
        self.assertFalse(self.errors)
        self.assertEqual(self.observer.network_diagnostics()['counts']['target_enable_failed'], 4)

    def test_unrelated_target_is_resumed_before_detach(self):
        self.event('Target.attachedToTarget', {'sessionId': 'unrelated', 'targetInfo': {'type': 'page'}})
        commands = self.observer._socket.messages
        resume = next(index for index, command in enumerate(commands)
                      if command['method'] == 'Runtime.runIfWaitingForDebugger' and command.get('sessionId') == 'unrelated')
        detach = next(index for index, command in enumerate(commands)
                      if command['method'] == 'Target.detachFromTarget' and command['params']['sessionId'] == 'unrelated')
        self.assertLess(resume, detach)
        self.assertNotIn('unrelated', self.observer._sessions)

    def test_main_document_ready_is_not_emitted_for_child_frame_or_worker(self):
        ready = []
        self.observer.document_ready.connect(lambda: ready.append(True))
        for kind in ('iframe', 'worker', 'shared_worker', 'service_worker'):
            self.event('Target.attachedToTarget', {'sessionId': kind, 'targetInfo': {'type': kind}})
            self.event('Page.domContentEventFired', {'timestamp': time.monotonic()}, kind)
        self.assertEqual(ready, [])
        self.event('Page.domContentEventFired', {'timestamp': time.monotonic()})
        self.assertEqual(ready, [True])

    def test_old_socket_document_ready_cannot_open_current_document_gate(self):
        ready = []
        self.observer.document_ready.connect(lambda: ready.append(True))
        old = self.observer._socket
        self.observer.set_page(FakePage())
        current = FakeSocket(parent=self.observer)
        self.observer._socket, self.observer._active = current, True
        message = json.dumps({'method': 'Page.domContentEventFired', 'params': {'timestamp': time.monotonic()}})
        self.observer._socket_callback(old, self.observer._receive_text, message)
        self.assertEqual(ready, [])
        self.observer._socket_callback(current, self.observer._receive_text, message)
        self.assertEqual(ready, [True])

    def test_media_requests_are_deduplicated_per_session_and_cleared_on_reset(self):
        self.observer._enabled = True
        self.observer._sessions.add('child')
        for session in ('', 'child'):
            self.event('Network.requestWillBeSent', request(), session)
            self.event('Network.responseReceived', response(mime='application/octet-stream'), session)
            self.event('Network.responseReceivedExtraInfo', {'requestId': 'r1', 'statusCode': 200,
                'headers': {'Content-Type': 'video/mp4', 'Content-Length': '123'}}, session)
            self.event('Network.requestWillBeSentExtraInfo', {'requestId': 'r1', 'headers': {'User-Agent': 'Fixture UA'}}, session)
            self.event('Network.responseReceived', response(mime='video/mp4'), session)
            self.event('Network.loadingFinished', {'requestId': 'r1', 'encodedDataLength': 123}, session)
        self.assertEqual(self.observer.network_diagnostics()['counts']['media_requests'], 2)
        self.assertEqual(len(self.observer._media_requests), 2)
        self.observer.reset()
        self.assertEqual(self.observer.network_diagnostics()['counts'], {})
        self.assertFalse(self.observer._media_requests)
        self.event('Network.responseReceived', response(mime='video/mp4'))
        self.assertEqual(self.observer.network_diagnostics()['counts'].get('media_requests', 0), 0)
        self.event('Network.requestWillBeSent', request(request_id='new-round'))
        self.event('Network.responseReceived', response(request_id='new-round', mime='video/mp4'))
        self.assertEqual(self.observer.network_diagnostics()['counts']['media_requests'], 1)

    def test_foreign_unknown_session_is_not_observed(self):
        self.event('Network.requestWillBeSent', request(), session='not-owned')
        self.assertFalse(self.observations)
        self.assertFalse(self.observer._store.records)

    def test_default_contexts_include_main_iframe_and_oopif_sessions(self):
        self.observer._enabled = True
        self.observer._sessions.add('child')
        for context_id, frame, session in ((1, 'main', ''), (2, 'same-process-frame', ''), (3, 'cross-origin-frame', 'child')):
            self.event('Runtime.executionContextCreated', {'context': {'id': context_id, 'auxData': {'isDefault': True, 'frameId': frame}}}, session)
        self.event('Runtime.executionContextCreated', {'context': {'id': 99, 'auxData': {'isDefault': False, 'type': 'isolated', 'frameId': 'main'}}})
        self.observer.scan_dom()
        commands = [command for command in self.observer._socket.messages if command['method'] == 'Runtime.evaluate']
        self.assertEqual({command['params']['contextId'] for command in commands}, {1, 2, 3})
        for command in commands:
            self.answer(command, {'result': {'value': {'page_url': 'https://page.example/', 'timestamp': time.time(), 'videos': []}}})
        self.assertEqual({snapshot['frame_id'] for snapshot in self.dom}, {'main', 'same-process-frame', 'cross-origin-frame'})

    def test_stale_context_is_dropped_on_destroy_or_navigation(self):
        self.event('Runtime.executionContextCreated', {'context': {'id': 1, 'auxData': {'isDefault': True, 'frameId': 'main'}}})
        self.event('Runtime.executionContextDestroyed', {'executionContextId': 1})
        self.assertFalse(self.observer._contexts)
        self.event('Runtime.executionContextCreated', {'context': {'id': 2, 'auxData': {'isDefault': True, 'frameId': 'main'}}})
        self.event('Runtime.executionContextsCleared', {})
        self.assertFalse(self.observer._contexts)

    def test_dom_keeps_blob_and_specific_video_encrypted_events_without_keys(self):
        self.observer._enabled = True
        self.event('Runtime.executionContextCreated', {'context': {'id': 1, 'auxData': {'isDefault': True, 'frameId': 'main'}}})
        self.observer.scan_dom()
        command = self.observer._socket.messages[-1]
        blob = 'blob:https://page.example/opaque'
        self.answer(command, {'result': {'value': {'page_url': 'https://page.example/watch', 'timestamp': time.time(), 'videos': [
            {'src': blob, 'current_src': blob, 'media_keys': True, 'encrypted': True,
             'events': [{'type': 'encrypted', 'timestamp': time.time()}]}]}}})
        self.assertEqual(self.dom[0]['videos'][0]['current_src'], blob)
        self.assertTrue(self.dom[0]['videos'][0]['encrypted'])
        self.assertNotIn('initData', json.dumps(self.dom))

    def test_dom_event_before_current_round_is_filtered(self):
        self.observer._enabled = True
        self.event('Runtime.executionContextCreated', {'context': {'id': 1, 'auxData': {'isDefault': True, 'frameId': 'main'}}})
        self.observer.scan_dom()
        command = self.observer._socket.messages[-1]
        self.answer(command, {'result': {'value': {'videos': [{'events': [
            {'type': 'seeking', 'timestamp': self.observer._round_started - 10},
            {'type': 'playing', 'timestamp': time.time()}]}]}}})
        self.assertEqual([event['type'] for event in self.dom[0]['videos'][0]['events']], ['playing'])

    def test_dom_poll_does_not_stack_same_context_requests(self):
        self.observer._enabled = True
        self.event('Runtime.executionContextCreated', {'context': {'id': 1, 'auxData': {'isDefault': True, 'frameId': 'main'}}})
        self.observer.scan_dom()
        self.observer.scan_dom()
        self.assertEqual(len([command for command in self.observer._socket.messages if command['method'] == 'Runtime.evaluate']), 1)

    def test_dom_scan_waits_for_a_real_default_context_with_nonempty_frame(self):
        self.observer._enabled = True
        self.observer.scan_dom()
        self.event('Page.domContentEventFired', {})
        self.event('Runtime.executionContextCreated', {'context': {'id': 1, 'auxData': {'isDefault': True}}})
        self.event('Runtime.executionContextCreated', {'context': {'id': 2, 'auxData': {
            'isDefault': False, 'type': 'isolated', 'frameId': 'main'}}})
        self.observer.scan_dom()
        self.assertFalse(any(command['method'] == 'Runtime.evaluate' for command in self.observer._socket.messages))
        self.assertFalse(self.dom)
        self.event('Runtime.executionContextCreated', {'context': {'id': 3, 'auxData': {'isDefault': True, 'frameId': 'main'}}})
        self.observer.scan_dom()
        commands = [command for command in self.observer._socket.messages if command['method'] == 'Runtime.evaluate']
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]['params']['contextId'], 3)
        self.answer(commands[0], {'result': {'value': {'frame_id': '', 'videos': []}}})
        self.assertEqual([snapshot['frame_id'] for snapshot in self.dom], ['main'])

    def test_destroyed_or_cleared_dom_context_drops_its_pending_reply(self):
        self.observer._enabled = True
        for context_id, event_name in ((11, 'Runtime.executionContextDestroyed'), (12, 'Runtime.executionContextsCleared')):
            with self.subTest(event=event_name):
                self.event('Runtime.executionContextCreated', {'context': {'id': context_id, 'auxData': {
                    'isDefault': True, 'frameId': 'main'}}})
                self.observer.scan_dom()
                command = self.observer._socket.messages[-1]
                self.assertEqual(command['method'], 'Runtime.evaluate')
                self.assertEqual(command['params']['contextId'], context_id)
                self.event(event_name, {'executionContextId': context_id} if event_name.endswith('Destroyed') else {})
                self.answer(command, {'result': {'value': {'page_url': 'https://page.example/old',
                    'timestamp': time.time(), 'videos': [{'current_src': 'https://cdn.example/old-video'}]}}})
                self.assertFalse(self.dom)
                self.assertNotIn(('', context_id), self.observer._contexts)

    def test_reused_context_id_with_different_frame_drops_old_dom_reply(self):
        self.observer._enabled = True
        self.event('Runtime.executionContextCreated', {'context': {'id': 13, 'auxData': {
            'isDefault': True, 'frameId': 'old-frame'}}})
        self.observer.scan_dom()
        old = self.observer._socket.messages[-1]
        self.event('Runtime.executionContextDestroyed', {'executionContextId': 13})
        self.event('Runtime.executionContextCreated', {'context': {'id': 13, 'auxData': {
            'isDefault': True, 'frameId': 'new-frame'}}})
        self.answer(old, {'result': {'value': {'videos': [{'current_src': 'https://cdn.example/old-video'}]}}})
        self.assertFalse(self.dom)
        self.observer.scan_dom()
        current = self.observer._socket.messages[-1]
        self.assertNotEqual(current['id'], old['id'])
        self.answer(current, {'result': {'value': {'videos': [{'current_src': 'https://cdn.example/current-video'}]}}})
        self.assertEqual(len(self.dom), 1)
        self.assertEqual(self.dom[0]['frame_id'], 'new-frame')
        self.assertEqual(self.dom[0]['videos'][0]['current_src'], 'https://cdn.example/current-video')

    def test_bounded_response_body_command_then_update(self):
        self.event('Network.requestWillBeSent', request())
        self.event('Network.requestWillBeSentExtraInfo', {'requestId': 'r1', 'headers': {'User-Agent': 'UA'}})
        self.event('Network.responseReceived', response())
        self.event('Network.loadingFinished', {'requestId': 'r1', 'encodedDataLength': 100})
        command = next(command for command in self.observer._socket.messages if command['method'] == 'Network.getResponseBody')
        self.answer(command, {'body': '#EXTM3U\n#EXTINF:2,\nabc000.png\n', 'base64Encoded': False})
        self.assertEqual(self.observations[-1].body, b'#EXTM3U\n#EXTINF:2,\nabc000.png\n')
        self.assertEqual(self.observer._body_inflight, 0)

    def test_body_inflight_and_reserved_budget_are_bounded(self):
        for index in range(12):
            request_id = str(index)
            self.event('Network.requestWillBeSent', request(request_id=request_id))
            self.event('Network.responseReceived', response(request_id=request_id, has_extra=False))
            self.event('Network.loadingFinished', {'requestId': request_id, 'encodedDataLength': 100})
        commands = [command for command in self.observer._socket.messages if command['method'] == 'Network.getResponseBody']
        self.assertEqual(len(commands), 4)
        self.assertEqual(self.observer._reserved_body_bytes, 4 * MAX_BODY_BYTES)

    def test_lookup_strictly_matches_own_page_id_instead_of_first_tab(self):
        old_socket = self.observer._socket
        self.observer._socket = None
        payload = [
            {'id': 'other-page', 'type': 'page', 'webSocketDebuggerUrl': 'ws://127.0.0.1:12345/devtools/page/other-page'},
            {'id': 'own-page', 'type': 'page', 'webSocketDebuggerUrl': 'ws://127.0.0.1:12345/devtools/page/own-page'}]
        reply = FakeReply(json.dumps(payload).encode())
        self.observer._reply = reply
        with patch('core.browser_observer.QWebSocket', FakeSocket):
            self.observer._finish_lookup(reply, self.observer._epoch)
        self.assertEqual(self.observer._socket.open_url, 'ws://127.0.0.1:12345/devtools/page/own-page')
        self.assertEqual(self.observer._socket.origin, 'http://127.0.0.1:12345')
        old_socket.deleteLater()

    def test_lookup_rejects_non_loopback_foreign_port_or_wrong_path(self):
        original_socket = self.observer._socket
        self.observer._socket = None
        for url in ('ws://localhost:12345/devtools/page/own-page', 'ws://evil.example:12345/devtools/page/own-page',
                    'ws://127.0.0.1:8888/devtools/page/own-page', 'ws://127.0.0.1:12345/devtools/browser/own-page'):
            with self.subTest(url=url):
                reply = FakeReply(json.dumps([{'id': 'own-page', 'type': 'page', 'webSocketDebuggerUrl': url}]).encode())
                self.observer._reply = reply
                with patch('core.browser_observer.QWebSocket', FakeSocket):
                    self.observer._finish_lookup(reply, self.observer._epoch)
                self.assertIsNone(self.observer._socket)
        self.observer._socket = original_socket

    def test_handshake_deadline_stops_even_without_any_socket_event(self):
        socket = self.observer._socket
        self.observer._enabled = False
        self.observer._connect_timeout.start(8000)
        self.observer._connection_deadline()
        self.assertFalse(self.observer._active)
        self.assertTrue(socket.aborted)
        self.assertFalse(self.observer._connect_timeout.isActive())
        self.assertEqual(self.errors, ['连接内嵌浏览器监听超时，请重新查找。'])

    def test_lookup_malformed_debugger_port_or_host_is_ignored(self):
        socket = self.observer._socket
        self.observer._socket = None
        for url in ('ws://127.0.0.1:invalid/devtools/page/own-page',
                    'ws://[broken/devtools/page/own-page'):
            with self.subTest(url=url):
                reply = FakeReply(json.dumps([{'id': 'own-page', 'type': 'page', 'webSocketDebuggerUrl': url}]).encode())
                self.observer._reply = reply
                self.observer._finish_lookup(reply, self.observer._epoch)
                self.assertIsNone(self.observer._socket)
        self.observer._socket = socket

    def test_lookup_read_limit_aborts_and_immediately_returns(self):
        socket = self.observer._socket
        self.observer._socket = None
        payload = [{'id': 'own-page', 'type': 'page',
                    'webSocketDebuggerUrl': 'ws://127.0.0.1:12345/devtools/page/own-page',
                    'padding': 'x' * (256 * 1024)}]
        reply = FakeReply(json.dumps(payload).encode())
        self.observer._reply = reply
        with patch('core.browser_observer.QWebSocket', FakeSocket) as factory:
            self.observer._finish_lookup(reply, self.observer._epoch)
        self.assertTrue(reply.aborted)
        self.assertTrue(reply.deleted)
        self.assertIsNone(self.observer._reply)
        self.assertIsNone(self.observer._socket)
        self.observer._socket = socket

    def test_command_timeout_releases_body_reservation(self):
        self.event('Network.requestWillBeSent', request())
        self.event('Network.responseReceived', response(has_extra=False))
        self.event('Network.loadingFinished', {'requestId': 'r1', 'encodedDataLength': 100})
        command = next(command for command in self.observer._socket.messages if command['method'] == 'Network.getResponseBody')
        epoch, _, callback = self.observer._commands[command['id']]
        self.observer._commands[command['id']] = (epoch, time.monotonic() - 6, callback)
        self.observer._expire_commands()
        self.assertEqual(self.observer._body_inflight, 0)
        self.assertEqual(self.observer._reserved_body_bytes, 0)

    def test_raw_protocol_and_server_error_is_not_user_facing(self):
        self.observer._receive_text('{invalid Cookie: hidden')
        self.observer._receive_text(json.dumps({'method': 'Runtime.consoleAPICalled', 'params': {'args': [{'value': 'Cookie: hidden'}]}}))
        self.assertFalse(self.errors)


class RuntimeTests(unittest.TestCase):
    def test_private_loopback_port_and_explicit_origin_whitelist_are_idempotent(self):
        with patch.object(browser_runtime, '_port', None), patch.dict(os.environ, {
                'QTWEBENGINE_REMOTE_DEBUGGING': '0.0.0.0:9222',
                'QTWEBENGINE_CHROMIUM_FLAGS': '--disable-gpu --remote-debugging-port=9222 --remote-allow-origins=*'}):
            port = browser_runtime.prepare_browser_runtime()
            self.assertGreater(port, 0)
            self.assertEqual(browser_runtime.prepare_browser_runtime(), port)
            self.assertEqual(os.environ['QTWEBENGINE_REMOTE_DEBUGGING'], f'127.0.0.1:{port}')
            flags = os.environ['QTWEBENGINE_CHROMIUM_FLAGS']
            self.assertIn(f'--remote-allow-origins=http://127.0.0.1:{port}', flags)
            self.assertNotIn('--remote-debugging-port', flags)
            self.assertNotIn('--remote-allow-origins=*', flags)
            self.assertIn('--disable-gpu', flags)
            for forbidden in ('--no-sandbox', '--disable-web-security', '--ignore-certificate-errors'):
                self.assertNotIn(forbidden, flags)

    def test_uninitialized_runtime_does_not_accept_external_browser_port(self):
        with patch.object(browser_runtime, '_port', None), patch.dict(os.environ, {'QTWEBENGINE_REMOTE_DEBUGGING': '9222'}):
            self.assertIsNone(browser_runtime.browser_runtime_port())

    def test_dom_scripts_do_not_request_keys_license_or_clicks(self):
        combined = DOM_OBSERVER_SCRIPT + DOM_SNAPSHOT_SCRIPT
        self.assertIn('video.currentSrc', combined)
        self.assertIn('video.videoWidth', combined)
        self.assertIn('video.mediaKeys', combined)
        self.assertIn("'encrypted'", combined)
        for forbidden in ('initData', '.click(', 'requestMediaKeySystemAccess', 'generateRequest', '.fetch(', 'XMLHttpRequest'):
            self.assertNotIn(forbidden, combined)


class DialogNavigationTests(unittest.TestCase):
    """模拟页面取消时序，真实 Chromium 的快速重导航由 discovery_smoke 覆盖。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from contextlib import ExitStack
        from ui import discovery_dialog as module
        self.module = module
        profile_class = module.QWebEngineProfile

        class Profile(QObject):
            HttpCacheType = profile_class.HttpCacheType
            PersistentCookiesPolicy = profile_class.PersistentCookiesPolicy

            def httpUserAgent(self):
                return getattr(self, 'user_agent', 'QtWebEngine/6.11.2 Chrome/140.0.0.0')

            def setHttpUserAgent(self, value):
                self.user_agent = value

            def settings(self):
                return self

            def setAttribute(self, attribute, value):
                pass

            def setHttpCacheType(self, value):
                self.cache = value

            def setPersistentCookiesPolicy(self, value):
                self.cookies = value

        class Scripts:
            def __init__(self):
                self.values = []

            def insert(self, script):
                self.values.append(script)

        class Page(QObject):
            loadFinished = Signal(bool)
            loadStarted = Signal()
            urlChanged = Signal(object)
            renderProcessTerminated = Signal(object, int)
            fullScreenRequested = Signal(object)

            def __init__(self, profile, parent):
                super().__init__(parent)
                self.profile = profile
                self.script_set = Scripts()
                self.actions = []

            def scripts(self):
                return self.script_set

            def triggerAction(self, action):
                self.actions.append(action)

            def load(self, url):
                self.loaded_url = url

            def url(self):
                from PySide6.QtCore import QUrl
                return getattr(self, 'loaded_url', QUrl(''))

        class View(QWidget):
            def setPage(self, page):
                self.current = page

            def page(self):
                return self.current

            def setZoomFactor(self, value):
                self.zoom = value

            def stop(self):
                pass

        class Observer(QObject):
            ready = Signal()
            document_ready = Signal()
            observation = Signal(object)
            dom = Signal(dict)
            error = Signal(str)
            status = Signal(str)

            def __init__(self, page, parent, **kwargs):
                super().__init__(parent)
                self.page = page
                self.max_body_bytes = kwargs.get('max_body_bytes')
                self.active = False
                self.scans = 0

            def set_page(self, page):
                self.stop()
                self.page = page

            def start(self):
                self.active = True

            def stop(self):
                self.active = False

            def reset(self):
                pass

            def scan_dom(self):
                self.scans += 1

        class Worker(QObject):
            candidates_changed = Signal(object, str, int)
            batch_ready = Signal(object, int)
            resolver_result = Signal(object, int)
            diagnostics_changed = Signal(dict, int)
            status = Signal(str)
            finished = Signal()

            def __init__(self, parent, **kwargs):
                super().__init__(parent)
                self.max_body_bytes = kwargs.get('max_body_bytes')
                self.generation = 0
                self.submissions = []

            def start(self):
                pass

            def reset(self):
                self.generation += 1
                self.submissions.clear()

            def submit(self, *args):
                self.submissions.append(args)

            def cancel(self):
                pass

        self.stack = ExitStack()
        for name, replacement in [('QWebEngineView', View), ('QWebEngineProfile', Profile),
                                  ('PrivatePage', Page), ('BrowserNetworkObserver', Observer),
                                  ('DiscoveryWorker', Worker)]:
            self.stack.enter_context(patch.object(module, name, replacement))
        self.dialog = module.DiscoveryDialog(page_url='https://page.example/first')

    def test_configured_capture_limit_reaches_observer_worker_and_history(self):
        parent = QWidget()
        parent.settings = {'browser_body_limit_mib': 24}
        dialog = self.module.DiscoveryDialog(parent, page_url='https://page.example/configured')
        try:
            self.assertEqual(dialog._max_body_bytes, 24 * 1024 * 1024)
            self.assertEqual(dialog.observer.max_body_bytes, dialog._max_body_bytes)
            self.assertEqual(dialog.worker.max_body_bytes, dialog._max_body_bytes)
            self.assertEqual(dialog._max_history_bytes, 96 * 1024 * 1024)
        finally:
            dialog.shutdown()
            dialog.worker.finished.emit()
            dialog.deleteLater()
            parent.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_complete_dash_json_reaches_worker_without_fetching_bare_tracks(self):
        from core.discovery_models import NetworkObservation
        from core.request_context import RequestContext
        payload = json.dumps({'data': {'dash': {'duration': 20, 'video': [{
            'id': 80, 'baseUrl': 'https://cdn.example/video.m4s', 'bandwidth': 1000000,
            'width': 1920, 'height': 1080, 'codecs': 'avc1.640028', 'mimeType': 'video/mp4',
            'SegmentBase': {'Initialization': '0-1251', 'indexRange': '1252-1631'}}],
            'audio': [{'id': 30280, 'baseUrl': 'https://cdn.example/audio.m4s',
                       'codecs': 'mp4a.40.2', 'mimeType': 'audio/mp4', 'bandwidth': 128000}]}}}).encode()
        observed = NetworkObservation('dash-json', RequestContext('https://api.example/playurl'),
            200, 'application/json', payload, resource_type='Fetch', body_complete=True)
        self.dialog._listening = True
        with patch.object(self.dialog, '_queue_fetch') as fetch:
            self.dialog._network(observed)
            fetch.assert_not_called()
        self.assertEqual(self.dialog.worker.submissions[-1], ('network', observed))

    def test_generic_json_fallback_keeps_discovery_and_receives_capture_limit(self):
        from core.discovery_models import NetworkObservation
        from core.request_context import RequestContext
        observed = NetworkObservation('other-json', RequestContext('https://api.example/player'),
            200, 'application/json', b'{"video_url":"https://cdn.example/direct.mp4"}',
            resource_type='XHR', body_complete=True)
        self.dialog._listening = True
        with patch.object(self.module, 'config_media_urls', return_value=['https://cdn.example/direct.mp4']) as scanner, \
                patch.object(self.dialog, '_queue_fetch') as fetch:
            self.dialog._network(observed)
            scanner.assert_called_once_with(observed.body, observed.context.final_url, observed.content_type,
                                            max_bytes=self.dialog._max_body_bytes)
            self.assertEqual(fetch.call_args.args[0].url, 'https://cdn.example/direct.mp4')

    def tearDown(self):
        self.dialog.shutdown()
        if not self.dialog._worker_done:
            self.dialog.worker.finished.emit()
        for _ in range(5):
            self.app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.stack.close()

    def test_repeated_find_uses_new_page_same_private_profile_view_observer(self):
        dialog = self.dialog
        original = dialog.page
        profile, view, observer = dialog.profile, dialog.view, dialog.observer
        dialog.find()
        first = dialog.page
        dialog.page_url.setText('https://different.example/second?token=keep')
        dialog.find()
        self.assertIsNot(first, original)
        self.assertIsNot(dialog.page, first)
        self.assertIs(dialog.profile, profile)
        self.assertIs(dialog.view, view)
        self.assertIs(dialog.observer, observer)
        self.assertIs(observer.page, dialog.page)
        self.assertIn(self.module.QWebEnginePage.WebAction.Stop, first.actions)
        self.assertEqual(dialog.worker.generation, 2)
        script = dialog.page.script_set.values[0]
        self.assertEqual(script.sourceCode(), DOM_OBSERVER_SCRIPT)
        self.assertEqual(script.worldId(), self.module.QWebEngineScript.ScriptWorldId.MainWorld)
        self.assertEqual(script.injectionPoint(), self.module.QWebEngineScript.InjectionPoint.DocumentCreation)
        self.assertTrue(script.runsOnSubFrames())

    def test_spa_work_change_resets_candidates_without_replacing_profile(self):
        dialog = self.dialog
        dialog.find()
        dialog.observer.ready.emit()
        dialog.page.loadFinished.emit(True)
        first='https://page.example/self?modal_id=111&from_tab_name=main'
        dialog._url_changed(self.module.QUrl(first))
        generation=dialog.worker.generation
        profile=dialog.profile
        dialog._candidates=[object()]
        dialog._network_history['previous']=object()
        dialog._last_dom={'page_url':first}
        dialog._url_changed(self.module.QUrl('https://page.example/self?modal_id=222&from_tab_name=main'))
        self.assertEqual(dialog.worker.generation,generation+1)
        self.assertFalse(dialog._candidates)
        self.assertFalse(dialog._network_history)
        self.assertFalse(dialog._last_dom)
        self.assertIs(dialog.profile,profile)

    def test_spa_tracking_query_does_not_clear_current_work(self):
        dialog=self.dialog
        dialog.find()
        dialog.observer.ready.emit()
        dialog.page.loadFinished.emit(True)
        dialog._url_changed(self.module.QUrl('https://page.example/self?modal_id=111&from_tab_name=main'))
        generation=dialog.worker.generation
        dialog._url_changed(self.module.QUrl('https://page.example/self?modal_id=111&from_tab_name=record'))
        self.assertEqual(dialog.worker.generation,generation)

    def test_spa_change_after_dom_ready_before_load_finished_invalidates_previous_work(self):
        dialog=self.dialog
        dialog.find()
        dialog.observer.ready.emit()
        dialog.observer.document_ready.emit()
        dialog._url_changed(self.module.QUrl('https://page.example/self?modal_id=111'))
        generation=dialog.worker.generation
        self.assertFalse(dialog._page_loaded)
        dialog._url_changed(self.module.QUrl('https://page.example/self?modal_id=222'))
        self.assertEqual(dialog.worker.generation,generation+1)

    def test_spa_change_after_listening_stops_cannot_replay_previous_work(self):
        dialog=self.dialog
        dialog.find()
        dialog.observer.ready.emit()
        dialog.page.loadFinished.emit(True)
        dialog._url_changed(self.module.QUrl('https://page.example/self?modal_id=111'))
        dialog._listening=False
        dialog._network_history['previous']=object()
        generation=dialog.worker.generation
        dialog._url_changed(self.module.QUrl('https://page.example/self?modal_id=222'))
        self.assertEqual(dialog.worker.generation,generation+1)
        self.assertFalse(dialog._network_history)
        self.assertFalse(dialog._page_loaded)

    def test_cancelled_old_load_finished_does_not_open_new_dom_gate(self):
        dialog = self.dialog
        dialog.find()
        old = dialog.page
        dialog.page_url.setText('https://page.example/second')
        dialog.find()
        scans = dialog.observer.scans
        old.loadFinished.emit(False)
        self.assertFalse(dialog._page_loaded)
        self.assertEqual(dialog.observer.scans, scans)
        dialog.observer.dom.emit({'page_url': 'https://page.example/first', 'timestamp': time.time(), 'videos': []})
        self.assertFalse(dialog.worker.submissions)
        dialog.page.loadFinished.emit(True)
        dialog.observer.dom.emit({'page_url': 'https://page.example/second', 'timestamp': time.time(), 'videos': []})
        self.assertEqual(len([value for value in dialog.worker.submissions if value[0] == 'dom']), 1)

    def test_document_ready_opens_dom_gate_before_full_page_load(self):
        dialog = self.dialog
        dialog.find()
        dialog.observer.ready.emit()
        snapshot = {'page_url': 'https://page.example/first', 'timestamp': time.time(), 'videos': []}
        dialog.observer.dom.emit(snapshot)
        self.assertFalse(dialog._document_ready)
        self.assertFalse(dialog._page_loaded)
        self.assertFalse(dialog.worker.submissions)
        dialog.observer.document_ready.emit()
        self.assertTrue(dialog._document_ready)
        self.assertFalse(dialog._page_loaded)
        dialog.observer.dom.emit(snapshot)
        self.assertEqual([item[0] for item in dialog.worker.submissions], ['dom'])
        dialog.page.loadFinished.emit(True)
        self.assertTrue(dialog._page_loaded)

    def test_document_ready_does_not_open_blank_or_inactive_page_gate(self):
        dialog = self.dialog
        dialog.observer.document_ready.emit()
        self.assertFalse(dialog._document_ready)
        dialog.find()
        dialog.observer.document_ready.emit()
        self.assertFalse(dialog._document_ready)
        dialog.observer.ready.emit()
        dialog.observer.document_ready.emit()
        self.assertTrue(dialog._document_ready)

    def test_new_navigation_closes_document_gate_and_rejects_old_dom_timestamp(self):
        dialog = self.dialog
        dialog.find()
        dialog.observer.ready.emit()
        dialog.observer.document_ready.emit()
        self.assertTrue(dialog._document_ready)
        old_time = dialog._navigation_dom_after - 1
        dialog.page.loadStarted.emit()
        self.assertFalse(dialog._document_ready)
        dialog.observer.document_ready.emit()
        self.assertTrue(dialog._document_ready)
        dialog.observer.dom.emit({'page_url': 'https://page.example/first', 'timestamp': old_time, 'videos': []})
        self.assertFalse(dialog.worker.submissions)
        dialog.observer.dom.emit({'page_url': 'https://page.example/first', 'timestamp': time.time(), 'videos': []})
        self.assertEqual([item[0] for item in dialog.worker.submissions], ['dom'])

    def test_old_renderer_failure_does_not_stop_current_round(self):
        dialog = self.dialog
        dialog.find()
        old = dialog.page
        dialog.find()
        old.renderProcessTerminated.emit('old-crash', 1)
        self.assertTrue(dialog._listening)
        self.assertTrue(dialog.observer.active)

    def test_external_dom_signal_connections_survive_find(self):
        dialog = self.dialog
        seen = []
        dialog.observer.dom.connect(seen.append)
        dialog.find()
        dialog.find()
        snapshot = {'page_url': 'https://page.example/first', 'timestamp': time.time(), 'videos': []}
        dialog.observer.dom.emit(snapshot)
        self.assertEqual(seen, [snapshot])

    def test_shutdown_waits_for_retired_pages_worker_and_profile(self):
        dialog = self.dialog
        dialog.find()
        dialog.find()
        self.assertEqual(dialog._pages_alive, 3)
        complete = []
        dialog.shutdown_complete.connect(lambda: complete.append(True))
        dialog.shutdown()
        self.assertFalse(complete)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.assertEqual(dialog._pages_alive, 0)
        self.assertTrue(dialog._profile_gone)
        self.assertFalse(complete)
        dialog.worker.finished.emit()
        self.assertEqual(complete, [True])

    def test_profile_release_counts_every_retired_page(self):
        class Counter:
            _pages_alive = 3
            _closing = True
            _page_gone = False
            _owns_profile = True

            def _finish_shutdown(self):
                pass

            class Profile:
                calls = 0

                def deleteLater(self):
                    self.calls += 1

            profile = Profile()
        owner = Counter()
        self.module.DiscoveryDialog._page_destroyed(owner)
        self.module.DiscoveryDialog._page_destroyed(owner)
        self.assertEqual(owner.profile.calls, 0)
        self.assertFalse(owner._page_gone)
        self.module.DiscoveryDialog._page_destroyed(owner)
        self.assertEqual(owner.profile.calls, 1)
        self.assertTrue(owner._page_gone)


if __name__ == '__main__':
    unittest.main()
