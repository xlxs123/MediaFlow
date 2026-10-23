"""Opt-in, read-only CDP trace. This module is not imported by application code.

Response summaries are evidence, never MediaCandidates or download inputs.
Raw headers, response bodies and URL query values are not written to disk.
"""
from __future__ import annotations

from collections import Counter, OrderedDict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, urlunsplit

from utils.security import Redactor


MAX_BODY = 2 * 1024 * 1024
MAX_REQUESTS = 4096
FOCUS = re.compile(r'playurl|x/player|dash|\.mpd(?:$|[?])|\.m3u8(?:$|[?])|playinfo', re.I)
MANIFEST_MIMES = {'application/dash+xml', 'application/vnd.apple.mpegurl',
                  'application/x-mpegurl', 'audio/mpegurl', 'audio/x-mpegurl'}


def safe_url(value):
    value = str(value or '')
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https'):
            return '[非 HTTP 资源]' if value else ''
        value = urlunsplit((parsed.scheme, parsed.netloc.rsplit('@', 1)[-1], parsed.path,
                            parsed.query, ''))
    except ValueError:
        return '[无效 URL]'
    return Redactor().redact(value)[:8192]


def url_id(value):
    return hashlib.sha256(str(value).encode('utf-8', 'replace')).hexdigest()


def _number(value):
    return value if type(value) in (int, float) and value >= 0 else None


def summarize_body(body, content_type=''):
    """Whitelist structural evidence only; do not export arbitrary JSON values."""
    if not isinstance(body, bytes) or len(body) > MAX_BODY:
        return {'kind': 'BODY_NOT_AVAILABLE'}
    prefix = body.lstrip(b'\xef\xbb\xbf \t\r\n')
    if prefix.startswith(b'#EXTM3U'):
        return {'kind': 'HLS', 'segment_entries': body.count(b'#EXTINF'),
                'master': b'#EXT-X-STREAM-INF' in body}
    if re.match(br'(?:<\?xml[^>]*>\s*)?<(?:[\w.-]+:)?MPD(?:\s|>)', prefix):
        return {'kind': 'DASH_XML', 'representations': len(re.findall(br'<(?:[\w.-]+:)?Representation(?:\s|>)', body))}
    if prefix[4:8] in (b'ftyp', b'moov', b'moof', b'styp', b'sidx'):
        return {'kind': 'MEDIA_BYTES', 'first_box': prefix[4:8].decode('ascii')}
    if len(prefix) > 376 and all(prefix[i] == 0x47 for i in (0, 188, 376)):
        return {'kind': 'MEDIA_BYTES', 'first_box': 'MPEG_TS'}
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return {'kind': 'HTML' if prefix[:100].lower().startswith((b'<html', b'<!doctype html')) else 'OTHER'}
    fields, references, track_counts = set(), [], {}
    pending, visited, truncated = [(payload, '$', 0, False)], 0, False
    allowed_fields = {'dash', 'durl', 'video', 'audio', 'segmentbase', 'segment_base',
                      'initialization', 'indexrange', 'index_range', 'baseurl', 'base_url',
                      'mpd', 'm3u8', 'manifest', 'playurl', 'playinfo'}
    url_fields = {'url', 'baseurl', 'base_url', 'backupurl', 'backup_url', 'mpd_url',
                  'm3u8_url', 'manifest_url'}
    while pending and visited < 6000:
        value, path, depth, in_media = pending.pop()
        visited += 1
        if depth > 14:
            truncated = True
            continue
        if isinstance(value, dict):
            for key, child in list(value.items())[:128]:
                lower = str(key).lower()
                # Only known structural names become exported paths.
                child_path = path + '.' + (lower if lower in allowed_fields | url_fields else '_')
                media = in_media or lower in ('dash', 'durl', 'mpd', 'm3u8', 'manifest')
                if lower in allowed_fields:
                    fields.add(lower)
                if lower in ('video', 'audio', 'durl') and isinstance(child, list):
                    track_counts[lower] = max(track_counts.get(lower, 0), len(child))
                if lower in url_fields and (media or lower in ('mpd_url', 'm3u8_url', 'manifest_url')):
                    links = child if isinstance(child, list) else [child]
                    for link in links[:16]:
                        if isinstance(link, str) and link.startswith(('http://', 'https://')):
                            if len(references) < 128:
                                references.append({'field': child_path, 'url': safe_url(link),
                                                   'url_sha256': url_id(link)})
                            else:
                                truncated = True
                if isinstance(child, (dict, list)):
                    pending.append((child, child_path, depth + 1, media))
        elif isinstance(value, list):
            pending.extend((child, path + '[]', depth + 1, in_media) for child in value[:128]
                           if isinstance(child, (dict, list)))
            truncated |= len(value) > 128
    kind = 'JSON_DASH' if 'dash' in fields else 'JSON_DURL' if 'durl' in fields else 'JSON_OTHER'
    if kind == 'JSON_OTHER' and references:
        kind = 'JSON_MEDIA_DESCRIPTION'
    return {'kind': kind, 'fields': sorted(fields), 'track_counts': track_counts,
            'references': references, 'summary_truncated': bool(truncated or pending)}


class MediaChainTrace:
    """Consumes copies/whitelisted fields; no browser commands or discovery writes."""

    def __init__(self, name, page_url, expected):
        self.name, self.page_url, self.expected = name, safe_url(page_url), expected
        self.requests = OrderedDict()
        self._active = {}
        self._pending_observations = OrderedDict()
        self.event_counts = Counter()
        self.candidates, self.inline, self.errors = [], [], []
        self.candidate_timeline = []
        self.candidate_transitions = []
        self._started = time.monotonic()
        self.truncated = False

    def _new(self, session, request_id, url='', method='', resource_type=''):
        if len(self.requests) >= MAX_REQUESTS:
            self.truncated = True
            return None
        trace_id = f'R{len(self.requests) + 1:04d}'
        record = {'id': trace_id, 'request_id': (session + ':' if session else '') + request_id,
                  'url': safe_url(url), 'url_sha256': url_id(url), 'method': str(method)[:16],
                  'resource_type': str(resource_type)[:32], 'status': None, 'content_type': '',
                  'content_length': None, 'decoded_data_bytes': None, 'encoded_transfer_bytes': None,
                  'loading_finished': False, 'loading_error': '', 'focus_matches': sorted(set(
                      match.group().lower() for match in FOCUS.finditer(url))),
                  'initiator': {}, 'observed_by_discovery': False, 'body_seen_by_discovery': False,
                  'context_complete': None, 'body_summaries': {}, 'candidate_ids': []}
        self.requests[trace_id] = record
        self._active[(session, request_id)] = record
        return record

    def record_cdp(self, message):
        if not isinstance(message, dict):
            return
        method, params = message.get('method', ''), message.get('params', {})
        if not isinstance(params, dict) or not str(method).startswith('Network.'):
            return
        self.event_counts[str(method)] += 1
        request_id, session = str(params.get('requestId', '')), str(message.get('sessionId', ''))
        if not request_id:
            return
        record = self._active.get((session, request_id))
        if method == 'Network.requestWillBeSent':
            request = params.get('request', {})
            if not isinstance(request, dict):
                return
            redirect = params.get('redirectResponse')
            if record and isinstance(redirect, dict):
                self._response(record, redirect)
                record['redirect_to'] = safe_url(request.get('url'))
            record = self._new(session, request_id, request.get('url', ''), request.get('method', ''), params.get('type', ''))
            if record is None:
                return
            initiator = params.get('initiator', {})
            if isinstance(initiator, dict):
                stack = initiator.get('stack', {})
                frames = stack.get('callFrames', []) if isinstance(stack, dict) else []
                record['initiator'] = {'type': str(initiator.get('type', ''))[:32],
                    'url': safe_url(initiator.get('url')), 'stack': [
                        {'url': safe_url(frame.get('url')), 'line': frame.get('lineNumber'),
                         'column': frame.get('columnNumber')} for frame in frames[:6] if isinstance(frame, dict)]}
            record['request_header_names'] = sorted(str(k).lower() for k in request.get('headers', {}))
            record['frame_id'] = str(params.get('frameId', ''))[:256]
            record['session_id'] = session[:256]
            record['timestamp'] = _number(params.get('timestamp'))
            record['wall_time'] = _number(params.get('wallTime'))
        elif record is not None:
            if method == 'Network.responseReceived':
                self._response(record, params.get('response', {}))
                record['resource_type'] = str(params.get('type') or record['resource_type'])
            elif method == 'Network.responseReceivedExtraInfo':
                headers = params.get('headers', {})
                if isinstance(headers, dict):
                    headers = {str(k).lower(): v for k, v in headers.items()}
                    record['wire_content_type'] = str(headers.get('content-type', ''))[:160]
            elif method == 'Network.dataReceived':
                count = _number(params.get('dataLength'))
                if count is not None:
                    record['decoded_data_bytes'] = (record['decoded_data_bytes'] or 0) + count
            elif method == 'Network.loadingFinished':
                record['loading_finished'] = True
                record['encoded_transfer_bytes'] = _number(params.get('encodedDataLength'))
            elif method == 'Network.loadingFailed':
                record['loading_error'] = Redactor().redact(str(params.get('errorText', '')))[:200]
        # The unchanged observer's Qt slot can run before this raw-CDP slot.
        # Reconcile its pending snapshot without creating duplicate requests.
        for key, observation in list(self._pending_observations.items()):
            if self._observation_record(observation) is not None:
                self._pending_observations.pop(key, None)
                self.record_observation(observation)

    @staticmethod
    def _response(record, response):
        if not isinstance(response, dict):
            return
        headers = {str(k).lower(): v for k, v in response.get('headers', {}).items()}
        record['status'] = _number(response.get('status'))
        record['content_type'] = str(headers.get('content-type') or response.get('mimeType', '')).split(';', 1)[0].lower()[:160]
        try:
            record['content_length'] = int(headers['content-length'])
        except (KeyError, TypeError, ValueError):
            pass
        if response.get('url'):
            record['response_url'] = safe_url(response['url'])
            record['response_url_sha256'] = url_id(response['url'])
        record['from_cache'] = bool(response.get('fromDiskCache') or response.get('fromServiceWorker'))

    def _observation_record(self, observation):
        identity, fingerprint = observation.request_id, url_id(observation.context.url)
        record = next((r for r in reversed(self.requests.values()) if r['request_id'] == identity
                       and fingerprint in (r['url_sha256'], r.get('response_url_sha256'))), None)
        return record

    def record_observation(self, observation):
        record = self._observation_record(observation)
        if record is None:
            self._pending_observations[(observation.request_id, url_id(observation.context.url))] = deepcopy(observation)
            while len(self._pending_observations) > 128:
                self._pending_observations.popitem(last=False)
                self.truncated = True
            return
        record['observed_by_discovery'] = True
        record['context_complete'] = observation.context_complete
        record['has_cookie'] = bool(observation.context.cookies)
        record['has_authorization'] = bool(observation.context.headers.get('authorization'))
        request_range = observation.context.headers.get('range', '')
        response_range = observation.response_headers.get('content-range', '')
        record['request_range'] = request_range if re.fullmatch(r'bytes\s*=\s*\d*\s*-\s*\d*', request_range, re.I) else ''
        record['response_content_range'] = response_range if re.fullmatch(r'bytes\s+[\d*]+-[\d*]+/[\d*]+', response_range, re.I) else ''
        record['is_partial_response'] = observation.status == 206 or bool(record['response_content_range'])
        record['observer_body_complete_flag'] = observation.body_complete
        if observation.body is not None:
            record['body_seen_by_discovery'] = True
            record['observed_body_bytes'] = len(observation.body)
            record['body_summaries']['existing_observer'] = summarize_body(observation.body, observation.content_type)
            if observation.body_complete and observation.resource_type in ('Fetch', 'XHR') and 200 <= observation.status < 300:
                # Reproduce the existing UI's pure URL extraction for diagnosis;
                # never submit any of these results to its fetch queue.
                from core.config_media import config_media_urls
                record['existing_config_url_scan'] = [
                    {'url': safe_url(url), 'url_sha256': url_id(url)} for url in
                    config_media_urls(observation.body, observation.context.final_url, observation.content_type)]

    def diagnostic_body(self, session, request_id, result, ok):
        """An optional CDP read of an already loaded response, not a network fetch."""
        import base64
        record = self._active.get((session, request_id))
        if record is None:
            return
        if not ok or not isinstance(result, dict) or not isinstance(result.get('body'), str):
            record['diagnostic_body_status'] = 'CDP_BODY_UNAVAILABLE'
            return
        text = result['body']
        if len(text) > MAX_BODY * 2:
            record['diagnostic_body_status'] = 'BODY_TOO_LARGE'
            return
        try:
            body = base64.b64decode(text, validate=True) if result.get('base64Encoded') else text.encode('utf-8')
        except (ValueError, UnicodeError):
            record['diagnostic_body_status'] = 'BODY_DECODE_FAILED'
            return
        if len(body) > MAX_BODY:
            record['diagnostic_body_status'] = 'BODY_TOO_LARGE'
            return
        record['diagnostic_body_status'] = 'READ_ONLY_NOT_SUBMITTED_TO_DISCOVERY'
        record['diagnostic_body_bytes'] = len(body)
        record['body_summaries']['diagnostic_read'] = summarize_body(body, record['content_type'])

    def record_inline(self, payload):
        if not isinstance(payload, dict):
            return
        text = payload.get('playinfo_json')
        if isinstance(text, str):
            evidence = {'source': 'window.__playinfo__', 'submitted_to_discovery': False,
                        'summary': summarize_body(text.encode('utf-8'))}
            if evidence not in self.inline:
                self.inline.append(evidence)

    def record_candidates(self, candidates, *_):
        result = []
        for candidate in candidates:
            result.append({'id': candidate.id, 'url': safe_url(candidate.final_url),
                'url_sha256': url_id(candidate.final_url), 'original_url_sha256': url_id(candidate.original_url),
                'protocol': candidate.media_type, 'manifest_type': candidate.manifest_type,
                'confidence': candidate.confidence, 'downloadable': candidate.downloadable,
                'fragment_only': bool(candidate.metadata.get('fragment_only')),
                'resource_role': str(candidate.metadata.get('resource_role', ''))[:100],
                'error_code': str(candidate.metadata.get('error_code', ''))[:100],
                'unavailable_reasons': candidate.safe_diagnostic()['unavailable_reasons'],
                'context_complete': candidate.context_complete, 'drm': candidate.drm,
                'last_request_id': candidate.metadata.get('last_request_id'),
                'request_range': candidate.request_context.headers.get('range', ''),
                'http_status': candidate.http_status,
                'body_complete': bool(candidate.metadata.get('body_complete'))})
        if result != self.candidates:
            before = {c['id']: c for c in self.candidates}
            for candidate in result:
                previous = before.get(candidate['id'])
                fields = ('protocol', 'downloadable', 'fragment_only', 'error_code')
                if previous and any(previous.get(k) != candidate.get(k) for k in fields):
                    self.candidate_transitions.append({'second': round(time.monotonic() - self._started, 3),
                        'candidate_id': candidate['id'], 'before': deepcopy(previous), 'after': deepcopy(candidate)})
            self.candidate_transitions = self.candidate_transitions[-512:]
            self.candidate_timeline.append({'update': len(self.candidate_timeline) + 1,
                'second': round(time.monotonic() - self._started, 3),
                'candidates': len(result), 'downloadable': sum(c['downloadable'] for c in result),
                'protocols': dict(Counter(c['protocol'] for c in result))})
            self.candidate_timeline = self.candidate_timeline[-128:]
        self.candidates = result

    def snapshot(self):
        records = deepcopy(list(self.requests.values()))
        descriptions = []
        for record in records:
            identities = {record['url_sha256'], record.get('response_url_sha256')}
            matched = [c for c in self.candidates if identities & {c['url_sha256'], c['original_url_sha256']}]
            record['candidate_ids'] = [c['id'] for c in matched]
            record['is_media'] = (record['resource_type'] == 'Media' or
                record['content_type'].startswith(('video/', 'audio/')) or record['content_type'] in MANIFEST_MIMES
                or any(c['protocol'] in ('Direct', 'HLS', 'DASH', 'Segment') for c in matched))
            hint = urlsplit(record['url']).path.lower().endswith(('.m4s', '.ts'))
            record['is_segment'] = any(c['fragment_only'] or c['protocol'] == 'Segment' for c in matched)
            record['segment_evidence'] = '现有候选分类' if record['is_segment'] else '后缀弱提示' if hint else '未确认'
            if hint and not matched:
                record['is_segment'] = None
            for source, summary in record['body_summaries'].items():
                if summary['kind'] not in ('HLS', 'DASH_XML', 'JSON_DASH', 'JSON_DURL', 'JSON_MEDIA_DESCRIPTION'):
                    continue
                references = deepcopy(summary.get('references', []))
                for ref in references:
                    ref['observed_request_ids'] = [r['id'] for r in records if ref['url_sha256'] in
                        (r['url_sha256'], r.get('response_url_sha256'))]
                    ref['candidate_ids'] = [c['id'] for c in self.candidates if ref['url_sha256'] in
                        (c['url_sha256'], c['original_url_sha256'])]
                descriptions.append({'request': record['id'], 'source': source, 'kind': summary['kind'],
                    'body_seen_by_discovery': record['body_seen_by_discovery'],
                    'candidate_ids_for_response': record['candidate_ids'], 'references': references})
            for ref in record.get('existing_config_url_scan', []):
                ref['observed_request_ids'] = [r['id'] for r in records if ref['url_sha256'] in
                    (r['url_sha256'], r.get('response_url_sha256'))]
                ref['candidate_ids'] = [c['id'] for c in self.candidates if ref['url_sha256'] in
                    (c['url_sha256'], c['original_url_sha256'])]
        counts = {'requests': len(records), 'fetch_xhr': sum(r['resource_type'] in ('Fetch', 'XHR') for r in records),
                  'media': sum(r['is_media'] for r in records), 'focus_requests': sum(bool(r['focus_matches']) for r in records),
                  'candidates': len(self.candidates), 'downloadable': sum(c['downloadable'] for c in self.candidates),
                  'segments': sum(c['fragment_only'] or c['protocol'] == 'Segment' for c in self.candidates)}
        kinds = {d['kind'] for d in descriptions} | {d['summary']['kind'] for d in self.inline}
        return {'case': self.name, 'page_url': self.page_url, 'historical_user_label': self.expected,
                'current_observation': 'DOWNLOADABLE_CANDIDATE' if counts['downloadable'] else
                    'SEGMENT_ONLY' if counts['segments'] else 'NO_DOWNLOADABLE_CANDIDATE',
                'counts': counts, 'event_counts': dict(self.event_counts), 'trace_truncated': self.truncated,
                'unmatched_observer_snapshots': len(self._pending_observations),
                'description_presence': {kind: kind in kinds for kind in ('HLS', 'DASH_XML', 'JSON_DASH', 'JSON_DURL')},
                'requests': records, 'descriptions': descriptions, 'inline_descriptions': deepcopy(self.inline),
                'candidates': deepcopy(self.candidates), 'candidate_timeline': deepcopy(self.candidate_timeline),
                'candidate_transitions': deepcopy(self.candidate_transitions),
                'errors': list(self.errors), 'external_downloads_performed': False,
                'raw_headers_or_response_bodies_saved': False}

    def write(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = self.snapshot()
        (directory / 'trace.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        for name, records in (('fetch-xhr', [r for r in snapshot['requests'] if r['resource_type'] in ('Fetch', 'XHR')]),
                              ('media', [r for r in snapshot['requests'] if r['is_media']]),
                              ('focus', [r for r in snapshot['requests'] if r['focus_matches']])):
            (directory / (name + '.jsonl')).write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records), encoding='utf-8')
        return snapshot
