"""真实轨道验收共用入口；通过现有发现服务与 DownloadWorker，不替换下载器。"""
from pathlib import Path
from types import SimpleNamespace

from core.discovery_models import NetworkObservation
from core.download_manager import DownloadManager
from core.downloader import DownloadRequest, DownloadWorker
from core.http_client import ContextHttpClient
from core.media_discovery import MediaDiscoveryService
from core.media_tracks import apply_selection, catalog, default_selection
from core.request_context import RequestContext


def discovered(fixture, kind):
    path = {'HLS': 'master.m3u8', 'DASH': 'dash/master.mpd', 'Direct': 'multi.mp4', 'Single': 'single.mp4'}[kind]
    context = RequestContext(fixture.base + path)
    with ContextHttpClient().open(context) as response:
        data = response.read(16 * 1024 * 1024)
        final = response.context
        mime = {'HLS': 'application/vnd.apple.mpegurl', 'DASH': 'application/dash+xml'}.get(kind, 'video/mp4')
        service = MediaDiscoveryService()
        service.consume_network(NetworkObservation('fixture', final, 200, mime, data,
            resource_type='Media' if kind in ('Direct', 'Single') else 'Fetch', body_complete=True,
            response_headers=response.response_headers))
    candidate = next(value for value in service.ranked(include_low=True) if value.url == context.url)
    return candidate, service


def request_for(fixture, directory, kind='DASH', preferences=None, selection=None, candidate=None):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    candidate = candidate or discovered(fixture, kind)[0]
    values = catalog(candidate)
    selection = selection if selection is not None else default_selection(values, preferences)
    candidate = apply_selection(candidate, selection)
    tools = fixture.tools
    template = DownloadRequest(str(tools / 'yt-dlp.exe'), candidate.url, str(directory),
        ffmpeg_path=str(tools / 'ffmpeg.exe'), ffprobe_path=str(tools / 'ffprobe.exe'),
        recovery_max_attempts=1, retry_base_delay=0)
    group = SimpleNamespace(id='actual-local-media', title=kind + ' Tracks', source_page='')
    request = DownloadManager._request(candidate, group, template)
    request.output_path = str(directory / (request.filename + '.' + request.output_extension))
    return request, candidate


def download(request):
    worker = DownloadWorker(request)
    logs = []
    worker.log.connect(logs.append)
    result = worker._run_once()
    if not result.get('success'):
        import sys
        print('Local track case failed:', result.get('error_code'), result.get('error'), '\n'.join(logs[-8:]), file=sys.stderr, flush=True)
    return result, logs
