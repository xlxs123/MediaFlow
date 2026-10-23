"""真实 HTTP / yt-dlp 验证分片与视频并行、AES 完整性和任务隔离。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QCoreApplication
from core.download_queue import DownloadQueue
from core.downloader import DownloadRequest
from core.yt_dlp_manager import detect_yt_dlp, detect_ffmpeg
from tests.hls_fixture import HlsFixture, FIXTURE_HEADERS, OBSERVED_HEADERS
from tests.integration_hls import probe, decoded_hash


def pump_until(app, condition, timeout=90):
    deadline = time.monotonic() + timeout
    while not condition():
        app.processEvents()
        if time.monotonic() >= deadline:
            raise RuntimeError('真实并发验收超时')
        time.sleep(0.01)
    app.processEvents()


def main():
    app = QCoreApplication.instance() or QCoreApplication([])
    directory = ROOT / 'verification'
    directory.mkdir(exist_ok=True)
    output = directory / 'queue-downloads' / str(time.time_ns())
    yt, ff = detect_yt_dlp(), detect_ffmpeg()
    assert yt.found and ff.found
    fixture = HlsFixture(directory / 'queue-media')
    fixture.generate()
    fixture.start()
    checks, queues, metrics = [], [], {}
    reference = decoded_hash(fixture.directory / 'direct.mp4')

    def request(name, headers, fragments=4, filename='same video'):
        fixture.parallel_headers[name] = headers.copy()
        return DownloadRequest(yt.path, fixture.base + f'/parallel/{name}/index.m3u8', str(output), filename,
                               headers, ff.path, inherit_range='range' in headers, concurrent_fragments=fragments)

    def verify(job):
        assert job.result and job.result['success'], (job.name, job.result, list(job.logs)[-10:])
        path = job.result['files'][0]
        probe(path)
        assert decoded_hash(path) == reference
        assert job.diagnostic['classification'] == 'HLS_AES128' and job.diagnostic['drm'] is False
        return path

    try:
        for number in (1, 4):
            queue = DownloadQueue(1)
            queues.append(queue)
            name = f'fragments-{number}'
            job = queue.add(request(name, OBSERVED_HEADERS, number, name))
            start = time.monotonic()
            queue.start()
            pump_until(app, lambda: queue.active_count == 0)
            verify(job)
            stats = fixture.parallel_stats[name]
            assert stats['headers_ok'] and stats['requests'] >= 4
            assert stats['peak'] == 1 if number == 1 else stats['peak'] >= 2, stats
            metrics[name] = {'elapsed_seconds': round(time.monotonic() - start, 3),
                             'peak_segment_requests': stats['peak'], 'configured_fragments': number}
        checks.append('fragment_parallelism_real_http_1_vs_4')
        checks.append('aes128_png_parallel_decoded_hash')

        fixture.parallel_delay = 1.0
        queue = DownloadQueue(3)
        queues.append(queue)
        contexts = [FIXTURE_HEADERS, OBSERVED_HEADERS,
                    {**OBSERVED_HEADERS, 'referer': 'https://another.fixture/watch', 'user-agent': 'Another Fixture Browser'},
                    {**FIXTURE_HEADERS, 'cookie': 'session=fixture-other-cookie', 'authorization': 'Bearer fixture-other-auth'}]
        jobs = [queue.add(request(f'batch-{i}', headers)) for i, headers in enumerate(contexts)]
        active = []
        starts = []
        seen = set()

        def changed(job_id):
            active.append(queue.active_count)
            job = queue.get(job_id)
            if job and job.worker is not None and job_id not in seen:
                seen.add(job_id)
                starts.append(job_id)

        queue.changed.connect(changed)
        queue.start()
        assert queue.active_count == 3 and jobs[3].state == 'queued'
        pump_until(app, lambda: queue.active_count == 0)
        paths = [verify(job) for job in jobs]
        assert starts == [job.id for job in jobs], starts
        assert max(active) == 3, active
        assert fixture.parallel_active_jobs_peak == 3, fixture.parallel_active_jobs_peak
        assert len(set(path.casefold() for path in paths)) == 4
        for i, job in enumerate(jobs):
            stats = fixture.parallel_stats[f'batch-{i}']
            assert stats['headers_ok'] and stats['peak'] >= 2, stats
            safe_text = '\n'.join(job.logs) + json.dumps(job.diagnostic) + json.dumps(job.result)
            assert not any(secret in safe_text for secret in ('fixture-private-cookie', 'fixture-private-auth',
                            'fixture-private-key', 'fixture-other-cookie', 'fixture-other-auth')), safe_text
        metrics['batch'] = {'video_limit': queue.max_concurrent, 'peak_workers': max(active),
                            'peak_http_video_overlap': fixture.parallel_active_jobs_peak,
                            'output_files': paths, 'task_names': [job.name for job in jobs]}
        checks.extend(['three_videos_limit_fifo_refill_real_http', 'per_task_headers_range_and_no_invented_origin',
                       'parallel_output_names_no_collision', 'per_task_log_diagnostic_secret_redaction'])

        fixture.parallel_delay = 2.0
        queue = DownloadQueue(3)
        queues.append(queue)
        cancelled = queue.add(request('cancel-one', OBSERVED_HEADERS, filename='cancel-one'))
        survivor = queue.add(request('survivor', FIXTURE_HEADERS, filename='survivor'))
        queue.start()
        pump_until(app, lambda: fixture.parallel_stats.get('cancel-one', {}).get('active', 0) > 0)
        queue.cancel(cancelled.id)
        pump_until(app, lambda: queue.active_count == 0)
        assert cancelled.result['cancelled'] and cancelled.worker is None
        verify(survivor)
        checks.append('stop_one_real_process_tree_other_job_completes')
        queue.retry(cancelled.id)
        pump_until(app, lambda: queue.active_count == 0)
        verify(cancelled)
        checks.append('retry_cancelled_real_partial_download')

        fixture.parallel_delay = 2.0
        queue = DownloadQueue(3)
        queues.append(queue)
        jobs = [queue.add(request(f'stop-all-{i}', OBSERVED_HEADERS, filename=f'stop-all-{i}')) for i in range(4)]
        queue.start()
        pump_until(app, lambda: any(fixture.parallel_stats.get(f'stop-all-{i}', {}).get('active', 0) > 0 for i in range(3)))
        queue.stop_all()
        pump_until(app, lambda: queue.active_count == 0)
        assert queue.paused and all(job.state == 'cancelled' for job in jobs)
        assert not any(hit['path'].startswith('/parallel/stop-all-3/') for hit in fixture.requests)
        checks.append('stop_all_cancels_workers_and_never_starts_waiting_job')

        report = {'passed': True, 'checks': checks, 'metrics': metrics, 'yt_dlp': yt.version, 'ffmpeg': ff.version}
        (directory / 'queue-integration-results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        (directory / 'queue-integration-results.json').write_text(json.dumps({'passed': False, 'checks': checks,
                                                                           'error': str(error)}, ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    finally:
        for queue in queues:
            queue.stop_all()
        pump_until(app, lambda: not any(queue.active_count for queue in queues), 25)
        fixture.close()


if __name__ == '__main__':
    raise SystemExit(main())
