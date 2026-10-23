"""Bounded browser-only checks on explicitly supplied public pages; no downloads."""
from __future__ import annotations

import base64
from collections import OrderedDict
import json
from pathlib import Path
import sys
import time

from PySide6.QtCore import QCoreApplication, QEvent, QTimer

from core.browser_runtime import browser_engine_info
from core.site_resolvers import ResolverRegistry
from ui.main_window import MainWindow
from utils.config import ConfigStore
from utils.security import Redactor


CASES = [
    ('Bilibili supplied video', 'https://www.bilibili.com/video/BV1CjaQ6NE42/?spm_id_from=333.1387.homepage.video_card.click&vd_source=80bbb013f28985933d971a814f5248d8'),
    ('Douyin supplied video', 'https://www.douyin.com/jingxuan?modal_id=7695103317542967537'),
    ('Rou homepage', 'https://rou.video/'),
]


def run_webview2_live(app, args):
    directory = Path(args[args.index('--webview2-live') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    app.setQuitOnLastWindowClosed(False)
    reports = []
    window = MainWindow(ConfigStore(directory / 'test-config.json'))
    window.showMinimized()
    for index, (name, url) in enumerate(CASES):
        dialog = window.open_discovery(page_url=url, activate=False)
        dialog.listen_seconds = 60
        dialog.worker.resolver_registry = ResolverRegistry([])
        dialog.browser_controls.set_large_mode(True)
        loaded, probes, errors, notices = [], [], [], []
        documents, media = set(), OrderedDict()
        maximum_videos = [0]
        def observation(value):
            if value.resource_type == 'Document' and value.status > 0:
                documents.add(value.status)
            if value.status > 0 and (value.resource_type == 'Media' or
                value.content_type.startswith(('video/', 'audio/')) or value.content_type in (
                    'application/dash+xml', 'application/vnd.apple.mpegurl', 'application/x-mpegurl')):
                media[value.request_id] = (value.status, value.content_type)
                while len(media) > 1024:
                    media.popitem(last=False)
        dialog.observer.observation.connect(observation)
        dialog.observer.dom.connect(lambda snapshot: maximum_videos.__setitem__(0,
            max(maximum_videos[0], len(snapshot.get('videos', [])))))
        # deep_find replaces the Page; observe the persistent view signal.
        dialog.view.loadFinished.connect(loaded.append)
        dialog.observer.error.connect(errors.append)
        dialog.showMinimized()
        dialog.deep_find()
        started = time.monotonic()
        played = probed = False
        while time.monotonic() - started < 45:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            elapsed = time.monotonic() - started
            if dialog._document_ready and elapsed > 8 and not played:
                dialog.page.runJavaScript('''(()=>{const list=[...document.querySelectorAll('video')].filter(v=>{
                    const r=v.getBoundingClientRect();return r.width>0&&r.height>0;});
                    for(const v of list.slice(0,1)){v.scrollIntoView({block:'center'});v.muted=true;v.play().catch(()=>{});}return list.length;})()''')
                played = True
            if elapsed > 33 and not probed and dialog.observer._enabled:
                probed = dialog.observer.probe_capabilities(probes.append)
            time.sleep(.01)
        if dialog.page.is_ready:
            dialog.page.runJavaScript("(()=>{const t=document.body?.innerText||'';return JSON.stringify({document_state:document.readyState,unsupported_html5:/不支持.{0,12}HTML5|HTML5.{0,12}不支持/i.test(t),verification:/安全验证|验证身份|验证码|访问异常|网络环境存在风险/.test(t),players:[...document.querySelectorAll('video')].slice(0,8).map(v=>({ready_state:v.readyState,error_code:v.error?.code||0,paused:v.paused,current_time:v.currentTime}))});})()", notices.append)
        end = time.monotonic() + 1
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(.01)
        capabilities = probes[-1] if probes else None
        states = (capabilities or {}).get('video_elements', {}).get('states', [])
        dialog.browser_controls.set_large_mode(False)
        app.processEvents()
        report = {'case': name, 'page_url': Redactor().redact(url),
            'final_url': Redactor().redact(dialog.page.url().toString()),
            'engine': browser_engine_info(dialog.profile), 'load_finished': loaded,
            'system_cookies_used': False, 'site_resolvers_used': False,
            'network': dialog.observer.network_diagnostics(), 'observer_errors': errors,
            'main_document_statuses': sorted(documents),
            'page_notices': json.loads(notices[-1]) if notices and notices[-1] else None,
            'capabilities': capabilities, 'video_count': maximum_videos[0],
            'player_initialized': any((v.get('ready_state') or 0) >= 1 for v in states),
            'video_error_codes': sorted({v['error_code'] for v in states if type(v.get('error_code')) is int}),
            'media_responses': len(media), 'media_statuses': sorted({v[0] for v in media.values()}),
            'media_mime_types': sorted({v[1] for v in media.values()}),
            'displayed_candidate_count': len(dialog._visible_candidates()),
            'candidate_count': len(dialog._candidates), 'candidates': [
                {'protocol': c.media_type, 'confirmed': c.confirmed, 'confidence': c.confidence,
                 'quality': c.qualityScore, 'http_status': c.http_status, 'drm': c.drm,
                 'source_type': c.source_type, 'context_complete': c.context_complete,
                 'discovery_state': c.discovery_state, 'validation_state': c.validation_state,
                 'download_state': c.download_state, 'downloadable': c.downloadable,
                 'unavailable_reasons': c.safe_diagnostic()['unavailable_reasons'],
                 'request_header_names': sorted(c.request_context.headers),
                 'has_cookie': bool(c.request_context.cookies),
                 'has_response_headers': bool(c.metadata.get('response_headers'))} for c in dialog._candidates],
            'discovery_counts': dialog.discovery_counts()}
        before_scores = [(c.id, c.confidence, list(c.confidenceReasons)) for c in dialog._candidates]
        low_ids = {c.id for c in dialog._candidates if c.confidence < 40}
        cards = dialog._candidate_cards
        reachable = set(cards)
        for card in cards.values():
            reachable.update(card.quality_combo.itemData(i) for i in range(card.quality_combo.count()))
        retained = all(c.id in reachable for c in dialog._candidates)
        collapsed = not dialog.include_low.isChecked() and all(cards[cid].isHidden() for cid in low_ids if cid in cards)
        dialog.include_low.setChecked(True)
        expanded = len(dialog._visible_candidates()) == len(dialog._candidates) and all(
            not dialog._candidate_cards[cid].isHidden() for cid in low_ids if cid in dialog._candidate_cards)
        selectable, downloadable = [], []
        for candidate in dialog._candidates:
            if candidate.id not in dialog._candidate_cards:
                owner = next((g for g in dialog._groups if any(c.id == candidate.id for c in dialog._choices_for_group(g))), None)
                if owner:
                    dialog._choose_quality(owner.id, candidate.id)
            dialog._select_card(candidate.id)
            if dialog.selected_candidate() and dialog.selected_candidate().id == candidate.id:
                selectable.append(candidate.id)
                assert dialog.confirm_button.isEnabled() == candidate.downloadable
                if candidate.downloadable:
                    downloadable.append(candidate.id)
                else:
                    assert candidate.unavailable_reasons
        report['selection_verification'] = {'discovered': len(dialog._candidates), 'selectable': len(selectable),
            'downloadable': len(downloadable), 'all_selectable': len(selectable) == len(dialog._candidates),
            'manual_low_downloadable': sum(c.id in downloadable and c.confidence < 40 for c in dialog._candidates),
            'external_downloads_performed': False}
        assert report['selection_verification']['all_selectable'], name + ' 候选选择回归'
        dialog.include_low.setChecked(False)
        scores_unchanged = before_scores == [(c.id, c.confidence, c.confidenceReasons) for c in dialog._candidates]
        report['candidate_presentation'] = {
            'recommended': sum(c.confidence >= 80 for c in dialog._candidates),
            'confirmation': sum(40 <= c.confidence < 80 for c in dialog._candidates),
            'low_confidence': len(low_ids), 'all_retained': retained,
            'low_collapsed_by_default': collapsed, 'low_expanded_successfully': expanded,
            'scores_unchanged': scores_unchanged,
            'passed': retained and collapsed and expanded and scores_unchanged}
        assert report['candidate_presentation']['passed'], name + ' 候选展示回归'
        dialog.grab().save(str(directory / f'candidate-ui-{index + 1}.png'))
        if low_ids:
            dialog.include_low.setChecked(True)
            app.processEvents()
            first_low = next((dialog._candidate_cards[cid] for cid in low_ids if cid in dialog._candidate_cards), None)
            if first_low is not None:
                dialog.candidate_card_scroll.ensureWidgetVisible(first_low)
                app.processEvents()
            dialog.grab().save(str(directory / f'candidate-ui-{index + 1}-expanded.png'))
            dialog.include_low.setChecked(False)
        reports.append(report)
        screenshots = []
        if dialog.observer._enabled:
            dialog.observer._send('Page.captureScreenshot', {'format':'png','captureBeyondViewport':False},
                callback=lambda value, ok: screenshots.append(value if ok else {}))
            end = time.monotonic() + 3
            while time.monotonic() < end and not screenshots:
                app.processEvents()
                time.sleep(.01)
            if screenshots and isinstance(screenshots[-1].get('data'), str):
                (directory / f'page-{index + 1}.png').write_bytes(base64.b64decode(screenshots[-1]['data']))
        # Reports never include network URLs, Cookies, response bodies or page logs.
        (directory / 'webview2-online-results.json').write_text(json.dumps({
            'qt_platform': app.platformName(), 'packaged': bool(getattr(sys, 'frozen', False)), 'cases': reports},
            ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('case','load_finished','video_count','player_initialized','media_responses','candidate_count')}, ensure_ascii=True), flush=True)
        closed = []
        dialog.shutdown_complete.connect(lambda: closed.append(True))
        dialog.reject()
        end = time.monotonic() + 12
        while time.monotonic() < end and not closed:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            time.sleep(.01)
        if not closed:
            raise RuntimeError('公开页面浏览器关闭超时。')
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    window.close()
    end = time.monotonic() + 12
    while time.monotonic() < end and not window._webview2_profile.host._finished_once:
        app.processEvents()
        time.sleep(.01)
    if not window._webview2_profile.host._finished_once:
        raise RuntimeError('公开页面持久浏览器宿主关闭超时。')
    return 0
