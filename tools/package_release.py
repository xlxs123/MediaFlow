"""只有最终 EXE 验收成功后才能生成交付包。"""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dist-dir', default='dist')
    parser.add_argument('--release-tag', default='')
    parser.add_argument('--portable-check-dir', default='verification/portable-check-discovery')
    args = parser.parse_args()
    distribution = ROOT / args.dist_dir
    tag = args.release_tag + '-' if args.release_tag else ''
    unit = json.loads((ROOT / 'verification' / 'unit-results.json').read_text(encoding='utf-8'))
    integration = json.loads((ROOT / 'verification' / 'integration-results.json').read_text(encoding='utf-8'))
    parallel = json.loads((ROOT / 'verification' / 'queue-integration-results.json').read_text(encoding='utf-8'))
    gui = json.loads((ROOT / 'verification' / 'packaged' / 'gui-results.json').read_text(encoding='utf-8'))
    native = json.loads((ROOT / 'verification' / 'packaged-native' / 'gui-results.json').read_text(encoding='utf-8'))
    relocated = json.loads((ROOT / 'verification' / 'relocated' / 'gui-results.json').read_text(encoding='utf-8'))
    audit = json.loads((ROOT / 'verification' / 'protocol-audit.json').read_text(encoding='utf-8'))
    discovery_folders = ('discovery', 'discovery-native', 'packaged-discovery',
                         'packaged-discovery-native', 'relocated-discovery')
    discovery = {folder: json.loads((ROOT / 'verification' / folder / 'discovery-results.json').read_text(encoding='utf-8'))
                 for folder in discovery_folders}
    resolver_folders = ('resolver', 'resolver-native', 'packaged-resolver',
                        'packaged-resolver-native', 'relocated-resolver')
    resolver = {folder: json.loads((ROOT / 'verification' / folder / 'resolver-results.json').read_text(encoding='utf-8'))
                for folder in resolver_folders}
    batch_folders = ('batch', 'batch-native', 'packaged-batch', 'packaged-batch-native', 'relocated-batch')
    batch = {folder: json.loads((ROOT / 'verification' / folder / 'batch-results.json').read_text(encoding='utf-8'))
             for folder in batch_folders}
    rou_folders = ('rou-current', 'rou-current-native', 'packaged-rou-current',
                   'packaged-rou-current-native', 'relocated-rou-current')
    rou = {folder: json.loads((ROOT / 'verification' / folder / 'rou-current-results.json').read_text(encoding='utf-8'))
           for folder in rou_folders}
    live = json.loads((ROOT / 'verification' / 'rou-live-metadata.json').read_text(encoding='utf-8'))
    assert live['metadata_verified'] and not live['third_party_download_tested']
    assert live['resolver_trace']['api_status'] == 302 and live['resolver_trace']['container'] == 'PNG'
    assert live['resolver_trace']['playlist'] == 'VALID' and live['candidate']['resolver_verified']
    page_tools = json.loads((ROOT / 'verification' / 'page-tools-results.json').read_text(encoding='utf-8'))
    assert page_tools['passed'] and page_tools['tests'] >= 11 and page_tools['failures'] == page_tools['errors'] == page_tools['skipped'] == 0
    assert all(report['passed'] for report in (unit, integration, parallel, gui, native, relocated, audit))
    assert unit['failures'] == unit['errors'] == unit['skipped'] == 0
    assert all(report['passed'] for report in discovery.values())
    assert all(report['passed'] for report in resolver.values())
    assert all(report['passed'] for report in batch.values())
    assert gui['packaged'] and native['packaged'] and native['qt_platform'] == 'windows'
    assert relocated['packaged'] and relocated['qt_platform'] == 'windows'
    expected_download = 'GUI真实AES128 PNG分片到MP4'
    assert all(expected_download in report['checks'] for report in (gui, native, relocated)), '不能复用旧版 EXE 的验收报告'
    assert all('三类资源继承Range且不伪造Origin或凭据' in report['checks'] for report in (gui, native, relocated)), '必须重新验收第三站请求组合'
    assert all('GUI真实3视频4分片并行与等待补位' in report['checks'] for report in (gui, native, relocated)), '必须验收本版实际视频并行'
    assert all('MediaFlow名称与默认3视频4分片' in report['checks'] for report in (gui, native, relocated)), '必须验收新名称与并发默认值'
    assert all(report['peak_video_workers'] == 3 and report['default_concurrent_fragments'] == 4 for report in (gui, native, relocated))
    assert {'fragment_parallelism_real_http_1_vs_4', 'three_videos_limit_fifo_refill_real_http',
            'per_task_headers_range_and_no_invented_origin', 'stop_one_real_process_tree_other_job_completes',
            'retry_cancelled_real_partial_download', 'stop_all_cancels_workers_and_never_starts_waiting_job'} <= set(parallel['checks'])
    parallel_metrics = parallel['metrics']
    assert parallel_metrics['fragments-1']['peak_segment_requests'] == 1
    assert 2 <= parallel_metrics['fragments-4']['peak_segment_requests'] <= 4
    assert parallel_metrics['batch']['peak_workers'] == parallel_metrics['batch']['peak_http_video_overlap'] == 3
    assert {'aes128_png_segment_semantics', 'aes128_mixed_extension_segments', 'browser_metadata_opt_in',
            'origin_never_invented', 'observed_range_no_credentials', 'observed_range_cross_host',
            'observed_range_redirect', 'observed_range_isolation_403_not_drm'} <= {check['name'] for check in integration['checks']}
    assert Path(gui['output']).is_file() and Path(native['output']).is_file()
    exe = distribution / 'MediaFlow.exe'
    assert exe.is_file()
    exe_hash = digest(exe)
    build_source = json.loads((ROOT / 'verification/rou-pipeline-build-source.json').read_text(encoding='utf-8'))
    assert build_source['exe_sha256'] == exe_hash
    for path, sha256 in build_source['source_files'].items():
        assert digest(ROOT / path) == sha256, '打包后源码发生变化：' + path
    extracted = ROOT / args.portable_check_dir / 'MediaFlow' / 'MediaFlow.exe'
    assert digest(extracted) == exe_hash
    assert all(report.get('source_sha256') == exe_hash for report in (gui, native, relocated)), 'GUI报告必须来自当前EXE'
    required_discovery = {'自动查找入口与非模态父窗口浮窗无全局置顶',
        '内嵌页面真实播放与拖动后继续监听', '取消停止监听PageProfileWorker且不修改主输入',
        'HLS PNG内容识别及实际Headers / AES非DRM / 分片语义',
        'DASH实际网络与Manifest检测', 'Blob MSE通过Network关联真实HLS而非下载blob',
        'Master多清晰度Codecs帧率与默认1080p', '手动选择480p后持续更新保持用户选择',
        'VAST与ad-provider通用降权正片默认胜出并可查看广告',
        '真实FairPlay证据禁止确定且不请求许可证或密钥',
        '跨源iframe实际媒体与独立frame上下文', 'POST实际Method Body Authorization完整采集',
        '旧页面尚未加载完成连续查找不串DOM iframe或媒体请求',
        '确认仅导入候选完整上下文且不自动启动下载',
        '自动查找AES PNG Direct DASH POST均经现有管线真实下载与画面哈希一致',
        '同URL手动cURL替换自动查找上下文且不串用凭据',
        '配置日志候选详情不持久化请求凭据或完整签名'}
    for folder, result in discovery.items():
        assert len({case['case'] for case in result['cases']}) >= 15, folder
        assert required_discovery <= set(result['checks']), folder
        assert result['site_domain_rules'] == 0 and result['gui_ticks'] > 100, folder
        assert len(result['outputs']) == 5 and all(Path(output).is_file() for output in result['outputs']), folder
        if folder.startswith(('packaged', 'relocated')):
            assert result['packaged'] and result['source_sha256'] == exe_hash, folder
        if folder.endswith('native') or folder.startswith('relocated'):
            assert result['qt_platform'] == 'windows', folder
    required_resolver = {'当前API失败后legacy NEXT ev与roUd明文进入统一候选',
        '快速查找不Reload且使用当前DOM与已有Network历史',
        '深度查找先监听Ready再加载并隔离旧Page',
        'legacy roUd deflate与AES128 PNG分片Key语义及限定Query继承',
        'Resolver确认后经原队列yt-dlpFFmpeg真实AES解密画面哈希一致',
        '当前API无重定向后显式API JSON入口legacy兜底',
        'API明确返回index.jpg且不请求相邻猜测地址',
        '普通PNG缺少roUd拒绝且不猜邻近清单',
        '专用Resolver失败自动回到Generic Direct识别',
        '不同视频身份更新在下载前拒绝且部分文件不变',
        '更新合法签名后同任务真实续传不重下已完成分片且画面一致',
        '队列刷新媒体按钮实际重跑Resolver并确认后同任务断点续传',
        '用户手动隐藏普通遮挡且style滚动状态精确撤销',
        '混合500与403不突破每分片实际3次请求上限',
        'HTTP500暂时失败后第3次恢复并完成真实下载'}
    for folder, result in resolver.items():
        assert required_resolver <= set(result['checks']) and len(result['checks']) >= 24, folder
        assert result['gui_ticks'] > 100 and result['third_party_site_tested'] is False, folder
        assert result['metrics']['resume']['redownloaded_finished_fragments'] == 0, folder
        assert result['metrics']['retry']['actual_server_attempts'] == 3, folder
        assert len(result['outputs']) == 4 and all(Path(output).is_file() for output in result['outputs']), folder
        if folder.startswith(('packaged', 'relocated')):
            assert result['packaged'] and result['source_sha256'] == exe_hash, folder
        if folder.endswith('native') or folder.startswith('relocated'):
            assert result['qt_platform'] == 'windows', folder
    assert audit['downloader_site_domain_rules'] == 0
    required_batch = {'真实页面3个独立视频自动入队且不回填URL', 'Master多清晰度默认只下载最高可用版本',
        'VAST广告和缩略图不进入默认批量', '磁盘同名文件和批内同名在启动前续号占名',
        '单任务暂停继续保留同一文件和续传身份其他视频继续', '共享HTTP上限实际限制多个视频合计请求',
        '真实AES128 PNG分片Key继承Range且不伪造Origin凭据',
        '勾选组与所有清晰度真实下载编码尺寸正确父Master不重复',
        '超长中文标题实际输出仍保留清晰度标签且文件名长度有界',
        '一个批量任务真实HTTP500失败不终止其余任务且失败重试恢复',
        'Signed403只等待当前视频更新地址其他继续且同任务恢复',
        '同一DASH MPD归一组且所有清晰度按真实Representation过滤下载',
        '批量过期自动重跑原页Resolver同一任务续传不重下载完成分片'}
    for folder, result in batch.items():
        assert required_batch <= set(result['checks']) and len(result['checks']) >= 13, folder
        assert len(result['outputs']) == 15 and all(Path(output).is_file() for output in result['outputs']), folder
        assert result['gui_ticks'] > 100 and result['third_party_site_tested'] is False, folder
        limits = result['metrics']['global_http_limit']
        assert limits['configured'] == limits['controller_peak'] == 2 and limits['server_segment_peak'] <= 2, folder
        assert result['metrics']['auto_refresh'] == {'retries': 1, 'redownloaded_finished_fragments': 0}, folder
        if folder.startswith(('packaged', 'relocated')):
            assert result['packaged'] and result['source_sha256'] == exe_hash, folder
        if folder.endswith('native') or folder.startswith('relocated'):
            assert result['qt_platform'] == 'windows', folder
    required_rou = {'当前API302 Location精确保留与三种CDN目录PNG分片结构真实浏览器验证',
        'PNG Chunk roUd flag zlib HLS VOD全部可见且Resolver已验证100优先',
        '绝对分片三CDN和原始签名Query保持原样不猜文件名',
        'TSR无NEXT页面正片优先于广告MP4且缩略图302排除',
        '三视频四分片原队列批量真实下载同名续号且解码画面一致',
        '标准AES128 key和伪装PNG分片通过原HLS下载合并且画面一致',
        '同源API合法Cookie和跨CDN通配CORS不强制include凭据',
        '已加载Image的真实PNG响应经CDP取body且缺失Origin保持缺失',
        '真实FairPlay KEYFORMAT解析与下载guard拒绝且无密钥许可证请求',
        '单视频签名401自动复用原合法会话重新API解析并跨CDN目录续传不重下已完成分片',
        '诊断Trace配置脱敏且CookieAuthorization不落盘',
        'Resolver候选到批量请求完整传递manifest_text且下载时不重新GET signed index.png',
        'PNG传输地址强制按声明HLS分流并下载EXTINF PNG分片而非直接复制索引',
        '所有成功MP4经实际ffprobe验证MP4 family容器与真实视频流',
        '真实yt-dlp零退出PNG伪MP4被ffprobe判OUTPUT_NOT_VIDEO且队列FAILED',
        '正确HLS管线中服务器实际返回PNG分片同样FAILED不将图片拼接误报成功',
        '真实PNG分片roUd明文及zlib二进制TS载荷解包后合并为视频且画面哈希一致',
        'PNG分片roUd内标准AES128密文先解包再由原yt-dlp解密且画面一致',
        '过期刷新替换新manifest_text和最终base URL不复用旧正文'}
    for folder, result in rou.items():
        assert result['passed'] and required_rou <= set(result['checks']) and len(result['checks']) >= 25, folder
        assert result['metrics']['embedded_manifest'] == {'batch_transport_refetches': 0, 'decoded_hls_body_carried': True}, folder
        assert result['metrics']['png_segment_payload'] == {'flags': [0, 1],
            'clear_wrapped_segments_downloaded': 21, 'decoded_video_hash_matches': True}, folder
        assert result['gui_ticks'] > 100 and result['third_party_site_tested'] is False, folder
        assert len(result['outputs']) == 8 and all(Path(output).is_file() for output in result['outputs']), folder
        assert result['metrics']['auto_refresh'] == {'retries': 1, 'redownloaded_finished_fragments': 0,
            'same_in_memory_browser_profile': True, 'cdn_and_directory_changed': True}, folder
        if folder.startswith(('packaged', 'relocated')):
            assert result['packaged'] and result['source_sha256'] == exe_hash, folder
        if folder.endswith('native') or folder.startswith('relocated'):
            assert result['qt_platform'] == 'windows', folder
    browser_licenses = json.loads((ROOT / 'verification' / 'browser-licenses' / 'provenance.json').read_text(encoding='utf-8'))
    assert len(browser_licenses['files']) >= 100
    for license_file in browser_licenses['files']:
        source_notice = ROOT / 'verification' / 'browser-licenses' / license_file['file']
        delivered_notice = distribution / 'licenses' / 'QtWebEngine' / license_file['file']
        assert digest(source_notice) == digest(delivered_notice) == license_file['sha256']
    modified_files = json.loads((ROOT / 'verification' / 'modified-files.json').read_text(encoding='utf-8'))
    from core.output_validator import validate_output
    output_probe_counts = {}
    for folder, result in rou.items():
        for output in result['outputs']:
            validate_output(output, ffmpeg_path=str(distribution / 'tools' / 'ffmpeg.exe'), expected_extension='mp4')
        output_probe_counts[folder] = len(result['outputs'])
    assert modified_files['removed'] == []
    for changed in modified_files['changes']:
        assert digest(ROOT / changed['path']) == changed['sha256'], '修改清单需要刷新：' + changed['path']
    recovered = json.loads((ROOT / 'verification/rou-original-recovery.json').read_text(encoding='utf-8'))
    assert recovered['passed'] and recovered['originals_read_only'] and not recovered['network_used']
    assert len(recovered['samples']) == 4
    assert {(320, 192), (864, 864)} <= {tuple(item['source_png_dimensions']) for item in recovered['samples']}
    for item in recovered['samples']:
        assert item['source_unchanged']
        output = (ROOT / item['output']).resolve()
        assert output.is_relative_to((ROOT / 'verification/rou-recovered-existing').resolve())
        assert digest(output) == item['output_sha256']
        validate_output(output, ffmpeg_path=str(distribution / 'tools/ffmpeg.exe'), expected_extension='mp4')
    replay = json.loads((ROOT / 'verification/rou-original-pipeline-replay.json').read_text(encoding='utf-8'))
    assert replay['passed'] and replay['real_png_segments'] == 54 and replay['manifest_transport_refetches'] == 0
    assert replay['original_unchanged'] and not replay['external_network_used']
    assert replay['encoded_video_packet_hash_matches_recovered_copy']
    validate_output(ROOT / replay['output'], ffmpeg_path=str(distribution / 'tools/ffmpeg.exe'), expected_extension='mp4')
    report = {'passed': True, 'executable': str(exe.relative_to(ROOT)), 'sha256': exe_hash,
              'size': exe.stat().st_size, 'unit_tests': unit['tests'],
              'integration_checks': len(integration['checks']), 'queue_integration_checks': len(parallel['checks']),
              'application_name': 'MediaFlow', 'default_parallel_videos': 3, 'default_concurrent_fragments': 4,
              'default_global_http_limit': 12,
              'batch_checks': {folder: len(result['checks']) for folder, result in batch.items()},
              'batch_output_count': len(batch['relocated-batch']['outputs']),
              'packaged_gui_checks': len(gui['checks']),
              'native_gui_checks': len(native['checks']), 'native_qt_platform': native['qt_platform'],
              'portable_relocation_checks': len(relocated['checks']), 'portable_hash_matches': True,
              'discovery_checks': {folder: len(result['checks']) for folder, result in discovery.items()},
              'discovery_scenarios': len({case['case'] for case in discovery['relocated-discovery']['cases']}),
              'resolver_checks': {folder: len(result['checks']) for folder, result in resolver.items()},
              'rou_current_checks': {folder: len(result['checks']) for folder, result in rou.items()},
              'rou_current_route': 'api_hls / actual Location / PNG-roUd HLS text / PNG-roUd binary media / existing downloader',
              'rou_current_output_count': 8, 'signed_refresh_finished_fragment_redownloads': 0,
              'real_rou_metadata_verified': True, 'real_rou_segment_count': live['resolver_trace']['segment_count'],
              'real_rou_duration': live['resolver_trace']['duration'], 'real_rou_download_tested': False,
              'real_rou_metadata_source': 'prior release metadata report; not a current complete download',
              'embedded_manifest_transport_refetches': 0, 'output_validation': 'actual ffprobe before success',
              'rou_outputs_reprobed_at_packaging': output_probe_counts,
              'png_output_false_success_regression': True,
              'png_segment_roud_payload_unpacked': True,
              'existing_rou_files_recovered_and_probed': len(recovered['samples']),
              'existing_rou_files_modified': False,
              'existing_rou_recovery_used_network': False,
              'real_existing_png_segments_replayed_through_full_pipeline': replay['real_png_segments'],
              'real_payload_replay_online_download': False,
              'private_recovered_videos_in_release': False,
              'exe_bound_to_current_core_ui_source': True,
              'page_tools_checks': page_tools['tests'],
              'new_unit_tests': unit['tests'] - modified_files['baseline_unit_tests'],
              'resolver_site_adapters': ['rou.video'], 'downloader_site_domain_rules': 0,
              'third_party_site_tested': False,
              'third_party_site_tested_scope': 'complete download; real Rou metadata parsing is reported separately',
              'gpl_source_copied': False,
              'browser_licenses': len(browser_licenses['files']), 'qt_version': browser_licenses['qt_version'],
              'site_domain_rules': 0, 'site_domain_rules_scope': 'downloader_and_generic_discovery',
              'modified_files': len(modified_files['changes']),
              'protocol_audit_passed': audit['passed'],
              'yt_dlp': gui['yt_dlp'], 'ffmpeg': gui['ffmpeg']}
    qa_failures = sorted((ROOT / 'verification' / 'qa-failures').rglob('*.json'))
    if qa_failures:
        report['qa_retry_notes'] = [
            '历史失败记录保留；本轮源码Rou原生监听曾超时，队列集成曾在启动检测工具阶段失败，触发原因未确定。',
            '完整复测保留原断言和等待时限，最终发行验收核对当前EXE哈希；失败记录不计为通过。']
    target = ROOT / 'verification' / 'release-results.json'
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = (f'# 实际测试结果\n\n'
               f'- 单元测试：{unit["tests"]} 项通过，0 失败、0 错误、0 跳过。\n'
               f'- 本地 HLS / AES-128 / DASH / HTTP 真实回归：{len(integration["checks"])} 项通过。\n'
               f'- 本地视频队列 / 分片并行 / 单项与全部停止 / 重试：{len(parallel["checks"])} 项通过。\n'
               f'- 打包 EXE 后台 GUI：{len(gui["checks"])} 项通过。\n'
               f'- 打包 EXE Windows 原生 Qt 平台：{len(native["checks"])} 项通过，窗口最小化运行。\n'
               f'- 便携 ZIP 解压到另一目录后运行：{len(relocated["checks"])} 项通过，EXE 哈希一致。\n'
               + ''.join(f'- 自动查找 {folder}：{len(result["checks"])} 项通过。\n' for folder, result in discovery.items()) +
               ''.join(f'- Resolver / 浏览器操作 / 刷新续传 {folder}：{len(result["checks"])} 项通过。\n' for folder, result in resolver.items()) +
               ''.join(f'- 一键批量 / 质量分组 / 隔离与续传 {folder}：{len(result["checks"])} 项通过，15个输出。\n' for folder, result in batch.items()) +
               ''.join(f'- 当前 Rou API / PNG / 原队列 / 合法会话自动刷新 {folder}：{len(result["checks"])} 项通过，8个输出。\n' for folder, result in rou.items()) +
               f'- 页面操作真实 Chromium 脚本：{page_tools["tests"]} 项通过。新增单测 {report["new_unit_tests"]} 项。\n' +
               f'- 自动查找独立网页场景：{report["discovery_scenarios"]} 类；源码、打包版及迁移版均实际运行内嵌 Chromium。\n'
               f'- Qt {report["qt_version"]} / Chromium 官方许可资料：{report["browser_licenses"]} 页，哈希核对通过。\n'
               f'- 协议与域名旧规则检查：{len(audit["checks"])} 项通过。\n'
               f'- yt-dlp：{gui["yt_dlp"]}；FFmpeg：{gui["ffmpeg"]}。\n'
               f'- EXE SHA256：`{report["sha256"]}`。\n\n'
               '所有下载测试使用 FFmpeg 自生成视频和 localhost HTTP 服务器。'
               '真实运行 yt-dlp/FFmpeg，使用 ffprobe 验证视频、音频和时长。\n\n'
               '自动查找覆盖真实网页 Direct206、Clear/AES128 HLS、PNG分片、DASH、Blob/MSE、480/720/1080p、'
               '签名和重定向、VAST广告、thumbnail/poster过滤、多播放器、DRM拒绝、空页、跨源iframe及POST。'
               '实际WebM播放和拖动产生DOM事件；HLS/DASH测试页发出真实网络请求。'
               '每候选独立保留实际Headers、Cookie、Authorization、body、Range及完整签名，确认只导入，'
               'AES/PNG/Direct/DASH/POST五个自动捕获源通过原队列下载，解码画面哈希与源视频一致。'
               '测试包含取消清理、重复查找、手动清晰度保留和同URL的cURL上下文替换，配置与日志没有凭据。\n\n'
               '上述验收全部使用本地自生成媒体，未把它描述为第三方站点登录或播放器兼容实测。'
               '规则置信度不是统计概率；只有孤立分片无法保证还原完整视频，'
               '第三方页面仍可能要求用户登录/点击播放或限制内嵌浏览器。\n\n'
               '应用名：MediaFlow。默认同时下载3个视频、每视频4个分片。'
               f'真实 HTTP 验收记录分片并发1时峰值{parallel_metrics["fragments-1"]["peak_segment_requests"]}、'
               f'并发4时峰值{parallel_metrics["fragments-4"]["peak_segment_requests"]}；视频请求同时重叠3项，第四项等待并补位。'
               '不同任务的会话头、Range、无Origin请求、进度和日志隔离，同名输出互不覆盖；'
               '停止单项后其他任务仍完成、部分文件重试成功、停止全部不派发等待任务。'
               '输出解码画面 SHA256 与源视频相同；模拟延迟的结果不能推算实际网站提速倍数。\n\n'
               '原生界面截图已核对：Windows 深色系统主题下，数字框、任务队列与表头使用统一浅色调色板，文字清晰可读。\n\n'
               '协议重构验收：AES-128 不误报 DRM；manifest/key/segment 继承必要会话 Headers；'
               '403 独立诊断；Widevine/FairPlay/PlayReady 与初始化 PSSH 正确拒绝；'
               'AES-128 解码画面 SHA256 与原始视频相同。'
               '无后缀 DASH Template、POST body、跨 origin、重定向及 HEAD 均有实际回归。\n\n'
               '新增分片兼容验收：VOD AES-128 的 abc000.png / abc001.png 及 bin/dat/无后缀/其他名称'
               '分片保持 Media Segment 语义，解码画面哈希相同；隔离模式下 Manifest Range 不传到 key/普通分片；'
               '浏览器元数据默认不传播、显式兼容可用，Origin 不会自动伪造。\n\n'
               '第三站补充验收：只有 Referer、User-Agent、Range: bytes=0-；三类资源均返回 206，'
               'Range 继承、跨主机及 307 重定向真实下载通过，解码画面相同；'
               '不发送 Origin/Cookie/Authorization。关闭继承时 key 的 403 为 HTTP_403、drm=false。'
               '分片自身字节范围优先，既有 Range 隔离模式仍有实际下载回归。\n\n'
               '旧页面兼容验收：当前API明确失败后NEXT ev或API显式JSON、明文/deflate PNG roUd、限定query继承、普通PNG拒绝、'
               '快速不Reload、深度先监听、导航缩放大屏、手动遮挡与完整撤销、真实刷新媒体按钮全部通过。'
               '签名过期后保留两个已完成分片，更新URL和原播放页均不重下；不同视频身份在写入前拒绝。'
               'HTTP500、500/403混合错误最多实际请求3次，临时失败第3次恢复成功，输出画面哈希一致。'
               '这些完整下载、旧页面兼容和续传验收使用测试专用localhost映射；真实Rou元数据解析另行报告。'
               '独立实现协议解析，没有复制第三方 GPL 源码。Downloader无网站分支，EXT-X-KEY与扩展名旧误判无残留。\n\n'
               '批量下载验收：3个独立视频、HLS默认最高质量、VAST广告与缩略图过滤、勾选与全清晰度模式、'
               'DASH两个实际Representation输出、磁盘及队列同名占名、单任务暂停继续、失败隔离与重试、'
               'Signed403等待及合法Resolver自动刷新均有真实下载。全局HTTP上限设为2时，多个视频合计请求峰值为2。'
               '默认上限12，默认3个视频、每个4个分片；不同任务有独立输出、上下文及临时目录。'
               '降级默认询问，只有勾选允许降级才自动使用下一可用质量；所有清晰度模式不重复降级。'
               '签名去重需要同资源与视频身份/DOM关系证据，没有证据的token不能只凭路径合并。\n\n'
               '初次打包的 QtWidgets DLL 报错根因是 PATH 中 Poppler ICU78 与系统 ICU 的同名冲突。'
               'spec 现隔离构建 PATH、排除冲突的系统替代库；修复后 EXE 两种 Qt 平台均已通过实际下载验收。\n')
    if qa_failures:
        summary += ('\n验收过程记录：保留历史失败、本轮源码Rou原生监听超时和队列集成工具启动检测失败。'
                    '触发原因未确定；完整复测保留原断言和等待时限。'
                    '最终本地回归串行完整重跑，发布校验核对当前EXE哈希。'
                    '源码包保留 `verification/qa-failures` 中的脱敏记录。\n')
    summary += ('\n当前 Rou 主线验收：TSR无NEXT页面，实际API302 Location；标准PNG Chunk CRC、roUd flag、'
                '有界zlib解包为HLS VOD；三CDN绝对PNG分片、AES128、缺失Origin、广告与thumbnail排除。'
                '单视频签名401使用原内存浏览器会话重跑API，CDN和目录变化后核验同视频布局并续传，'
                '已完成两个分片零重下。既有批量3视频4分片、Direct/DASH/cURL与通用发现继续通过回归。'
                '18项实现说明见ROU_CURRENT_PROTOCOL.md；用户提供三种真实站点结构的协议单测不等同于在线下载。\n')
    summary += (f'\n真实 rou.video 播放页元数据解析通过：API302 / PNG(roUd) / flag0x01 / zlib / HLS VOD，'
                f'{live["resolver_trace"]["segment_count"]}个分片、{live["resolver_trace"]["duration"]:.6f}秒，'
                'DRM No、来源RouVideoResolver、已验证100。脱敏记录在verification/rou-live-metadata.json。'
                '这是前一正式解析版留下的元数据记录，本次未从真实站点下载整段视频；完整下载、AES、并发及续传使用自生成媒体验证。\n')
    summary += ('\n本次管线修复：Candidate/DownloadRequest显式传递manifest_text；嵌入式清单重复GET使用内存正文，'
                '批量索引重请求为0。PNG分片roUd明文/zlib媒体载荷先解包，标准AES128密文先解包再交原yt-dlp解密，'
                '输出画面哈希一致，实际MP4容器和视频流通过ffprobe。普通PNG两条负例均OUTPUT_NOT_VIDEO/FAILED，PNG流remux成MP4也拒绝。'
                '只读发现4个真实旧坏文件，包含320x192/864x864样本；分别有89/54/737/106个PNG容器，roUd内为明文TS，'
                '已完整解包并remux恢复独立H264/AAC MP4副本，原文件哈希未变。恢复副本不收入交付ZIP。'
                '样本A的54个真实PNG分片另经localhost重放至Resolver候选→原DownloadWorker完整管线，'
                '实际视频数据包哈希与恢复副本一致，index重请求0次，继承Referer/UA/Range且无Origin/Cookie/Authorization。'
                '这证明漏解包PNG分片是现有文件的真实根因；本次网络访问TLS失败，不能把原文件恢复称为新的实站在线下载。'
                '匿名证据见verification/rou-original-recovery.json。\n')
    (ROOT / 'verification' / 'TEST_RESULTS.md').write_text(summary, encoding='utf-8')
    for document in ('README.md', 'PROTOCOL_REFACTOR.md', 'MEDIA_DISCOVERY.md', 'RESOLVER_UPGRADE.md', 'ROU_CURRENT_PROTOCOL.md', 'ROU_PIPELINE_FIX.md', 'BATCH_DOWNLOAD.md', 'THIRD_PARTY_NOTICES.md', 'LICENSE'):
        (distribution / document).write_bytes((ROOT / document).read_bytes())
    (distribution / 'TEST_RESULTS.md').write_bytes((ROOT / 'verification' / 'TEST_RESULTS.md').read_bytes())
    (distribution / 'MODIFIED_FILES.md').write_bytes((ROOT / 'verification' / 'MODIFIED_FILES.md').read_bytes())
    with zipfile.ZipFile(ROOT / ('MediaFlow-' + tag + '便携版.zip'), 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(distribution.rglob('*')):
            if path.is_file():
                bundle.write(path, Path('MediaFlow') / path.relative_to(distribution))
        bundle.write(target, 'MediaFlow/release-results.json')
    source_files = []
    for folder in ('ui', 'core', 'utils', 'tests', 'assets'):
        source_files.extend(path for path in (ROOT / folder).rglob('*') if path.is_file() and '__pycache__' not in path.parts)
    for name in ('main.py', 'requirements.txt', 'README.md', 'MEDIAFLOW_BRAND_THUMBNAILS.md', 'PROTOCOL_REFACTOR.md', 'MEDIA_DISCOVERY.md', 'RESOLVER_UPGRADE.md', 'ROU_CURRENT_PROTOCOL.md', 'ROU_PIPELINE_FIX.md', 'BATCH_DOWNLOAD.md', 'build.ps1', 'MediaFlow.spec', 'M3U8 Downloader.spec',
                 '启动开发版.vbs', 'THIRD_PARTY_NOTICES.md', 'LICENSE', '.gitignore'):
        source_files.append(ROOT / name)
    source_files.extend(path for path in (ROOT / 'tools').iterdir() if path.is_file() and path.suffix != '.exe' and path.suffix != '.zip')
    for name in ('unit-results.txt', 'unit-results.json', 'integration-results.json', 'queue-integration-results.json',
                 'protocol-audit.json', 'release-results.json', 'TEST_RESULTS.md', 'modified-files.json', 'MODIFIED_FILES.md', 'page-tools-results.json', 'rou-live-metadata.json',
                 'rou-pipeline-original-outputs.json', 'rou-original-container-scan.json', 'rou-original-recovery.json',
                 'rou-original-pipeline-replay.json', 'rou-pipeline-build-source.json'):
        source_files.append(ROOT / 'verification' / name)
    for folder in ('gui', 'gui-native', 'packaged', 'packaged-native', 'relocated'):
        source_files.extend(path for path in (ROOT / 'verification' / folder).iterdir() if path.is_file())
    for folder in discovery_folders:
        source_files.extend(path for path in (ROOT / 'verification' / folder).iterdir()
                            if path.is_file() and (path.suffix == '.png' or path.name == 'discovery-results.json'))
    for folder in resolver_folders:
        source_files.append(ROOT / 'verification' / folder / 'resolver-results.json')
    for folder in batch_folders:
        source_files.extend(path for path in (ROOT / 'verification' / folder).iterdir()
                            if path.is_file() and (path.suffix == '.png' or path.name == 'batch-results.json'))
    for folder in rou_folders:
        source_files.extend(path for path in (ROOT / 'verification' / folder).iterdir()
                            if path.is_file() and (path.suffix == '.png' or path.name == 'rou-current-results.json'))
    source_files.extend(path for path in (ROOT / 'verification' / 'browser-licenses').iterdir() if path.is_file())
    source_files.extend(qa_failures)
    source_files.extend(path for path in (ROOT / 'verification' / 'rou-pipeline-baseline').iterdir()
                        if path.is_file() and path.suffix in ('.json', '.md'))
    with zipfile.ZipFile(ROOT / ('MediaFlow-' + tag + '完整源码.zip'), 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(source_files):
            bundle.write(path, Path('MediaFlow-source') / path.relative_to(ROOT))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
