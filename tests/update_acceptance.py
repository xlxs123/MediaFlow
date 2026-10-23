"""Isolated, actual packaged-EXE update acceptance; never uses the user's data.

MediaFlow.exe --update-check DIR --update-package ZIP --update-old-release DIR
MediaFlow.exe --update-check DIR --update-restart

For a separate crash/recovery run, use a fresh DIR and add --update-interrupt
or --update-interrupt-after-commit. The deliberate crash exits with code 73;
the next --update-restart verifies and resumes that recorded operation.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import faulthandler
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import ssl
from urllib.parse import urlsplit
import sys
import threading
import time
import traceback
from unittest.mock import patch
import zipfile

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QMessageBox

from core.process_tree import ProcessTree
from core.update_manager import UpdateError, UpdateManager
from ui.update_dialog import UpdateDialog
from utils.build_info import runtime_identity


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _hash_tree(directory):
    directory = Path(directory)
    result = {}
    for path in sorted(directory.rglob('*')):
        if path.is_symlink() or path.is_junction():
            raise ValueError('验收目录中包含重解析路径，未继续读取。')
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = _hash_file(path)
    return result


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _seed_data(directory):
    """Real compatible stores plus ordinary files whose original bytes are checked."""
    from core.batch_store import BatchStore
    from core.media_history import HistoryStore
    from core.task_store import TaskStore
    from utils.config import ConfigStore

    directory.mkdir(parents=True, exist_ok=True)
    media = directory / 'media' / 'preserved-media.bin'
    media.parent.mkdir()
    media.write_bytes(b'MediaFlow update acceptance: preserve original media\0' * 4096)
    ConfigStore(directory / 'config.json').save({'output_directory': str(media.parent),
        'organization': {'filename_template': '{site}_{title}.{ext}', 'classification': 'none'}})
    with HistoryStore(directory / 'history.sqlite3') as history:
        history.record({'task_key': 'update-preserved-history', 'queue_id': 1,
            'title': '更新前历史记录', 'source_site': 'localhost', 'task_state': 'completed',
            'files': [{'path': str(media), 'validation_state': 'unverified'}]})
        history.record({'task_key': 'update-preserved-failure', 'queue_id': 2,
            'title': '保留失败记录', 'source_site': 'localhost', 'task_state': 'failed',
            'error': '本地验收保留记录'})
    batches = BatchStore(directory / 'batches.sqlite3')
    batches.close()
    if not TaskStore(directory / 'tasks.json').save([]):
        raise AssertionError('验收任务快照未能保存')
    profile = directory / 'BrowserProfile'
    profile.mkdir()
    (profile / 'acceptance-marker.txt').write_text('private fixture; no real login data', encoding='utf-8')
    (directory / '.mediaflow-data.lock').write_bytes(b'\0')


def _broken_package(source, target):
    """A hash-correct release with an intentionally invalid PE, not a fake pass."""
    with zipfile.ZipFile(source, 'r') as original:
        manifest = deepcopy(json.loads(original.read('release.json')))
        manifest.pop('signature', None)
        manifest.pop('package', None)
        manifest['build_id'] = manifest['build_id'][:72] + '-startup-failure'
        manifest['release_notes'] = '本地验收专用：故意不可执行的 EXE；必须失败并保留旧版。'
        build = json.loads(original.read('build-info.json'))
        build['build_id'] = manifest['build_id']
        replacements = {'MediaFlow.exe': b'Intentionally not a Windows executable.\n',
            'build-info.json': json.dumps(build, ensure_ascii=False, indent=2).encode('utf-8')}
        for entry in manifest['files']:
            if entry['path'] in replacements:
                data = replacements[entry['path']]
                entry.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        with zipfile.ZipFile(target, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as output:
            output.writestr('release.json', json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
            for entry in manifest['files']:
                name = entry['path']
                if name in replacements:
                    output.writestr(name, replacements[name])
                else:
                    with original.open(name, 'r') as incoming, output.open(name, 'w', force_zip64=True) as outgoing:
                        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
    return manifest


def run_update_acceptance(app, args):
    """Run only from the fixed --update-check test entry, with bounded waits."""
    def option(flag, default=''):
        if flag not in args:
            return default
        index = args.index(flag)
        if index + 1 >= len(args) or args[index + 1].startswith('--'):
            raise ValueError(f'{flag} 缺少参数')
        return args[index + 1]

    directory = Path(option('--update-check')).absolute()
    if directory.is_symlink() or directory.is_junction():
        return 2
    directory.mkdir(parents=True, exist_ok=True)
    directory = directory.resolve()
    online = _read_json(Path(option('--update-online-config'))) if '--update-online-config' in args else None
    if online:
        parsed = urlsplit(online.get('source_url', ''))
        if parsed.scheme != 'https' or parsed.hostname not in ('localhost', '127.0.0.1'):
            raise ValueError('本地集成验收仅允许明确的 localhost HTTPS 源。')
    restart = '--update-restart' in args
    interrupt = '--update-interrupt' in args or '--update-interrupt-after-commit' in args
    after_commit = '--update-interrupt-after-commit' in args
    mode = 'restart' if restart else 'interrupt-after-commit' if after_commit else 'interrupt' if interrupt else 'normal'
    result_path = directory / ('restart-result.json' if restart else 'interrupt-result.json' if interrupt else 'result.json')
    report = {'passed': False, 'mode': mode, 'runtime_identity': runtime_identity(), 'checks': [],
        'online_validation': '未配置真实在线源；本验收不声明在线发布服务通过',
        'publisher_authentication': '本地发布包未签名；只有哈希完整性通过和明确来源信任确认',
        'external_downloads': False, 'user_data_access': False, 'launched_processes': []}
    if online:
        report.update(online_validation='本地 HTTPS 集成测试；未验收公开在线更新源',
            publisher_authentication='Ed25519 临时测试密钥，独立注入本次隔离设置；不作为正式信任根',
            local_https=True, public_online_tested=False)
    started = time.monotonic()
    phase = ['启动']
    ticks, gaps = [], []
    owned_trees = []
    manager = dialog = None
    environment_before = {}
    session_path = directory / 'session.json'
    trace = (directory / (mode + '-trace.txt')).open('w', encoding='utf-8')

    # CPython 3.14.0 gh-140815: the native watchdog can itself crash while
    # dumping freed frames. External owned-process deadlines remain enforced.

    app.setQuitOnLastWindowClosed(False)
    timer = QTimer()
    timer.setInterval(25)

    def heartbeat():
        now = time.monotonic()
        if ticks:
            gaps.append({'seconds': now - ticks[-1], 'phase': phase[0]})
        ticks.append(now)
    timer.timeout.connect(heartbeat)
    timer.start()

    def save():
        _write_json(result_path, report)

    def check(condition, description, **evidence):
        if not condition:
            raise AssertionError(description + ': ' + repr(evidence))
        report['checks'].append({'description': description, 'at_seconds': round(time.monotonic() - started, 3), **evidence})
        save()

    def wait(predicate, seconds=120):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.005)
        raise TimeoutError('正式 EXE 更新验收等待超时：' + phase[0])

    def background(function, seconds=240):
        values = {}
        done = threading.Event()
        def execute():
            try:
                values['result'] = function()
            except Exception as error:
                values['error'] = error
            finally:
                done.set()
        threading.Thread(target=execute, daemon=True, name='update-acceptance').start()
        wait(done.is_set, seconds)
        if 'error' in values:
            raise values['error']
        return values.get('result')

    def ui_step(action, name, seconds=240):
        phase[0] = name
        before = len(ticks)
        check(bool(action()), name + '已发起')
        wait(lambda: not dialog.busy, seconds)
        report.setdefault('background_steps', []).append({'name': name, 'heartbeat_ticks': len(ticks) - before})
        return dialog.last_result

    def no_effect(action, description):
        check(action() is False and not dialog.busy, description)

    def history_count(path, immutable=False):
        uri = path.as_uri() + '?mode=ro' + ('&immutable=1' if immutable else '')
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as connection:
            return connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]

    def launch(previous=False):
        phase[0] = '实际打开旧版本' if previous else '实际打开新版本'
        version = session['old_version'] if previous else session['new_version']
        launch_private = directory / 'private' / ('launch-' + str(len(report['launched_processes']) + 1))
        launch_roaming, launch_local = launch_private / 'Roaming', launch_private / 'Local'
        def prepare_launch():
            data = launch_roaming / 'MediaFlow' / version
            data.mkdir(parents=True)
            launch_local.mkdir(parents=True)
            _write_json(data / 'migration-assistant.json', {'version': 1, 'handled': True,
                'result': 'private normal GUI acceptance fixture'})
        background(prepare_launch)
        manager.launch_env.update(APPDATA=str(launch_roaming), LOCALAPPDATA=str(launch_local))
        count_before = len(manager._launched)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.No):
            no_effect(dialog.launch_previous if previous else dialog.launch_new, '取消打开确认后未创建进程')
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes):
            outcome = ui_step(dialog.launch_previous if previous else dialog.launch_new, phase[0])
        check(outcome and outcome.get('started') and len(manager._launched) == count_before + 1,
            '确认后实际创建对应版本进程', result=outcome)
        process = manager._launched[-1]
        tree = ProcessTree(process)
        owned_trees.append(tree)
        expected_path = Path(session['old_release']) / 'MediaFlow.exe' if previous else Path(dialog.preview['target_path']) / 'MediaFlow.exe'
        private_db = launch_roaming / 'MediaFlow' / version / 'history.sqlite3'
        wait(lambda: process.poll() is not None or private_db.exists(), 90)
        check(process.poll() is None and private_db.is_file(), '独立 GUI 进程存活并实际初始化私有媒体库',
            version=version, pid=process.pid, history_path=str(private_db), executable=str(expected_path))
        alive_until = time.monotonic() + .25
        wait(lambda: time.monotonic() >= alive_until, 3)
        check(process.poll() is None, '实际 GUI 初始化后持续存活', version=version)
        check(background(lambda: history_count(private_db), 15) >= 0, '启动后媒体库实际可以只读查询', version=version)
        check(Path(outcome['path']).resolve() == expected_path.resolve(), '启动目标为预览中登记的真实 EXE', version=version)
        report['launched_processes'].append({'version': version, 'pid': process.pid, 'path': outcome['path'],
            'private_history': str(private_db), 'verified_alive': True})
        tree.terminate()
        tree.close()
        owned_trees.remove(tree)
        check(process.poll() is not None, '只结束本验收新建进程树', pid=process.pid)
        return outcome

    # All normal GUI launches use these private locations. No real profile is copied.
    private_roaming = directory / 'private' / 'Roaming'
    private_local = directory / 'private' / 'Local'
    try:
        check(report['runtime_identity'].get('packaged') is True, '验收运行于真实打包 EXE')
        check(report['runtime_identity'].get('app_version') == '2.1.0', '真实运行版本为 2.1.0')
        channel_state = report['runtime_identity'].get('release_channel_state')
        check(channel_state in ('pending', 'initialized'),
              '打包 EXE 读取有效的内置正式渠道状态', channel_state=channel_state)
        for name, value in (('APPDATA', private_roaming), ('LOCALAPPDATA', private_local),
                            ('QT_QPA_PLATFORM', 'offscreen')):
            environment_before[name] = os.environ.get(name)
            os.environ[name] = str(value)
        private_roaming.mkdir(parents=True, exist_ok=True)
        private_local.mkdir(parents=True, exist_ok=True)
        if restart:
            session = _read_json(session_path)
        else:
            if session_path.exists():
                raise ValueError('该验收目录已有 session.json，请使用新目录或 --update-restart。')
            package_path = Path(option('--update-package')).resolve()
            old_release = Path(option('--update-old-release')).resolve()
            if not package_path.is_file() or not (old_release / 'MediaFlow.exe').is_file():
                raise ValueError('请明确提供真实发布 ZIP 及保留旧版的完整发布目录。')
            phase[0] = '读取发布身份与旧目录基线'
            with zipfile.ZipFile(package_path, 'r') as archive:
                new_manifest = json.loads(archive.read('release.json'))
            old_identity = _read_json(old_release / 'build-info.json')
            old_data = directory / 'old-data'
            background(lambda: _seed_data(old_data))
            baseline = background(lambda: {'release': _hash_tree(old_release), 'data': _hash_tree(old_data)})
            session = {'old_release': str(old_release), 'old_data': str(old_data), 'package': str(package_path),
                'old_version': old_identity['app_version'], 'old_build': old_identity['build_id'],
                'new_version': new_manifest['version'], 'new_build': new_manifest['build_id'], 'baseline': baseline,
                'mode': mode}
            _write_json(session_path, session)
            check(len(baseline['release']) > 9 and len(baseline['data']) >= 6,
                '记录完整旧发布目录、数据库、任务、媒体与私有配置的真实 SHA-256 基线',
                release_files=len(baseline['release']), data_files=len(baseline['data']))
        for version in {session['old_version'], session['new_version']}:
            target = private_roaming / 'MediaFlow' / version
            target.mkdir(parents=True, exist_ok=True)
            marker = target / 'migration-assistant.json'
            if not marker.exists():
                _write_json(marker, {'version': 1, 'handled': True, 'result': 'private EXE acceptance fixture'})
        child_env = dict(os.environ)
        # A new onefile version must unpack independently of the test parent.
        child_env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
        manager = UpdateManager(state_dir=directory / 'updates', release_root=session['old_release'],
            current_identity={'app_version': session['old_version'], 'build_id': session['old_build']},
            current_data_dir=session['old_data'], health_data_root=directory / 'health-data', launch_env=child_env,
            ssl_context=ssl.create_default_context(cadata=online['ca_pem']) if online else None)
        # This suite intentionally exercises an unconfigured/local fixture.
        # The separate public suite uses the actual embedded formal channel.
        background(lambda: manager.save_settings({'source_url': ''}))
        if online:
            background(lambda: manager.save_settings({'trusted_keys': online['trusted_keys']}))
        dialog = UpdateDialog(manager)
        wait(lambda: not dialog.busy and dialog.manager is not None)
        check(not dialog.isVisible(), '验收界面保持后台，不占用用户前台')
        check(('待配置' if channel_state == 'pending' else '已固定') in dialog.channel_label.text(),
              '实际更新界面如实显示内置正式源与公钥状态', channel_state=channel_state)

        if restart:
            phase[0] = '重启恢复更新记录'
            records = background(manager.list_versions)
            identifier = session['operation_id']
            row = next((index for index, record in enumerate(dialog._versions) if record.get('id') == identifier), -1)
            check(row >= 0 and any(record.get('id') == identifier for record in records), '新 EXE 进程恢复原有版本记录')
            dialog.versions_table.selectRow(row)
            app.processEvents()
            check(dialog.preview and dialog.preview['id'] == identifier, '版本记录通过真实 UI 重新选中')
            previous_state = dialog.preview['state']
            check(previous_state in ('interrupted', 'deployed', 'healthy'), '中断或已完成部署恢复为可理解状态', state=previous_state)
            if previous_state == 'interrupted':
                dialog.deploy_confirm_check.setChecked(True)
                dialog.unsigned_confirm_check.setChecked(True)
                resumed = ui_step(dialog.deploy_update, '用户确认后重新部署中断版本')
                check(resumed and resumed['state'] == 'deployed', '中断部署实际恢复完成')
            restored = background(lambda: manager.deploy(identifier, confirmed=True, allow_unsigned=True))
            check(restored['state'] in ('deployed', 'healthy'), '重复同一更新操作幂等且重新验证已部署完整文件')
            check(len([record for record in background(manager.list_versions) if record.get('id') == identifier]) == 1,
                '重启和重复执行未重复创建同一更新记录')
            check(not manager._launched, '重启恢复没有自动启动任何新旧版本')
            report['operation'] = restored
        else:
            check('未配置在线更新源' in dialog.status_label.text(), '无真实源时显示未配置在线更新源')
            check(not dialog.startup_check.isChecked(), '首次启动检查默认关闭')
            checked = ui_step(dialog.check_updates, '无更新源手动检查')
            check(checked and checked['status'] == 'not_configured', '无源不伪报已是最新版本', result=checked)
            check(not dialog.download_button.isEnabled(), '无可信在线发布清单时禁止下载更新')
            before_import_ticks = len(ticks)
            if online:
                strict = UpdateManager(state_dir=directory/'normal-2x-policy',
                    current_data_dir=directory/'normal-2x-data',health_data_root=directory/'normal-2x-health')
                try:
                    background(lambda: strict.prepare_local(online['unsigned_package']))
                except UpdateError as error:
                    check('签名' in str(error), '正常2.0身份拒绝无签名本地包，普通确认不能绕过')
                else:
                    raise AssertionError('正常2.0身份接受了无签名本地包')
                def online_check(path, label):
                    dialog.source_edit.setText(online['source_url'].rsplit('/', 1)[0] + path)
                    return ui_step(dialog.check_updates, label)
                verified = online_check('/release.json', '真实 HTTPS 清单与 Ed25519 签名验证')
                check(verified.get('authenticated') is True and verified['status'] == 'update_available',
                    '独立可信测试公钥验证真实 HTTPS 清单后发现更新')
                check('已通过' in dialog.signature_label.text(), '签名通过状态真实显示')
                check(not dialog.preview and not manager._launched, '清单验签不等同于下载、部署或启动')
                for path in ('/tampered.json', '/unsigned.json', '/unknown-key.json'):
                    failed = online_check(path, 'HTTPS 签名负例 ' + path)
                    check(failed.get('passed') is False and not dialog.download_button.isEnabled(),
                        '错误或缺失签名不能通过普通确认按钮继续', case=path)
                timeout_before = dict(manager.network_timeouts)
                manager.network_timeouts.update(connect=3, check=.65)
                timed = online_check('/slow.json', '真实 HTTPS 总超时与 UI 响应')
                manager.network_timeouts = timeout_before
                check(timed.get('passed') is False and '时限' in timed.get('message', ''),
                    '阻塞网络受总时限约束且不会伪报最新版本')
                online_check('/bad-hash.json', '验签通过但 ZIP 哈希错误的清单')
                bad_hash = ui_step(dialog.download_update, '真实 HTTPS 错误哈希包拒绝')
                check(bad_hash.get('passed') is False and dialog.preview is None,
                    '完整 ZIP 与已签署哈希不符时停止安装')
                online_check('/cross-redirect.json', '已签署源的非信任重定向负例')
                bad_redirect = ui_step(dialog.download_update, '真实 HTTPS 跨来源重定向拒绝')
                check(bad_redirect.get('passed') is False and '来源' in bad_redirect.get('message',''),
                    '未授权跨来源目的地不被跟随')
                online_check('/release.json', '取消与重试前重新取得可信发布信息')
                def cancel_real_transfer():
                    accepted = dialog.download_update()
                    if accepted:
                        dialog._worker.progress.connect(lambda v: dialog.cancel()
                            if v.get('stage') == 'download' and v.get('completed',0)>0 else None)
                    return accepted
                cancelled = ui_step(cancel_real_transfer, '真实字节传输后取消下载')
                check(cancelled.get('passed') is False and '取消' in cancelled.get('message',''),
                    '取消操作停止本次 HTTPS 子进程')
                check(not list((manager.state_dir/'downloads').glob('*.zip')),
                    '校验失败和取消的未完成缓存被清理')
                preview = ui_step(dialog.download_update, '重新下载签名发布包与完整 ZIP 验证')
                check(preview and preview.get('authenticated') is True and '已通过' in dialog.integrity_label.text(),
                    '签名、ZIP 完整性与结构通过后才产生部署预览')
            else:
                preview = ui_step(lambda: dialog.import_local(session['package']), '完整发布 ZIP 后台导入与逐文件验证')
            check(preview and preview['state'] == 'prepared', '真实完整 ZIP 已生成预览')
            check(len(ticks) > before_import_ticks, 'ZIP I/O 与全文件哈希期间 Qt 心跳继续运行')
            check(preview['version'] == session['new_version'] and preview['build_id'] == session['new_build'],
                '预览与真实 ZIP 发布身份一致', version=preview['version'], build=preview['build_id'])
            check(preview['authenticated'] is bool(online),
                '在线签名认证与本地未签名包的可信度被正确区分')
            check(not Path(preview['target_path']).exists(), '导入与预览没有提前部署或执行 EXE')
            check(not manager._launched and not dialog.deploy_button.isEnabled(), '预览默认不执行部署或启动')
            no_effect(dialog.deploy_update, '未勾选部署确认时阻止部署')
            dialog.deploy_confirm_check.setChecked(True)
            if online:
                check(dialog.unsigned_confirm_check.isHidden() and dialog.deploy_button.isEnabled(),
                    '已验签包无需无签名绕过确认，仍须明确确认部署')
            else:
                no_effect(dialog.deploy_update, '仅部署确认不绕过本地包身份信任确认')
                dialog.unsigned_confirm_check.setChecked(True)
                check(dialog.deploy_button.isEnabled(), '两项明确确认后才允许部署未签名本地包')
            identifier = preview['id']
            session['operation_id'] = identifier
            _write_json(session_path, session)
            if interrupt:
                phase[0] = '真实进程在版本目录提交阶段中断'
                report.update(interruption_expected=True, interruption_exit_code=73,
                    interruption_point='after_commit' if after_commit else 'before_commit', operation_id=identifier)
                save()
                original_rename = os.rename
                def interrupted_rename(source, target, *values, **keywords):
                    if Path(target).resolve() == Path(preview['target_path']).resolve():
                        if after_commit:
                            original_rename(source, target, *values, **keywords)
                        os._exit(73)
                    return original_rename(source, target, *values, **keywords)
                with patch('core.update_manager.os.rename', side_effect=interrupted_rename):
                    ui_step(dialog.deploy_update, phase[0])
                raise AssertionError('本次进程没有到达约定的真实中断位置')
            deployed = ui_step(dialog.deploy_update, '确认后暂存并部署完整新版本')
            check(deployed and deployed['state'] == 'deployed', '正式 EXE 完成独立目录部署')
            target = Path(deployed['target_path'])
            check(target != Path(session['old_release']) and (target / 'MediaFlow.exe').is_file(), '新旧完整发布目录实际分离')
            check(deployed['migration_state'] and not manager._launched, '部署没有隐式执行数据迁移或启动程序',
                migration_state=deployed['migration_state'])
            no_effect(dialog.check_health, '部署后仍需单独确认执行新 EXE')
            phase[0] = '准备健康检查的真实私有数据库与快照'
            health_directory = directory / 'health-data' / session['new_version']
            background(lambda: _seed_data(health_directory))
            health_baseline = background(lambda: _hash_tree(health_directory))
            dialog.execute_confirm_check.setChecked(True)
            health = ui_step(dialog.check_health, '实际启动新 EXE 有限健康检查', 150)
            report['health'] = health
            check(health and health.get('passed') is True, '实际新 EXE 启动健康检查通过',
                identity=(health or {}).get('identity'), message=(health or {}).get('message'))
            check(health.get('identity', {}).get('packaged') is True and
                health['identity']['build_id'] == session['new_build'], '健康结果来自实际打包的新构建')
            check(background(lambda: _hash_tree(health_directory)) == health_baseline,
                '真实 EXE 健康检查保留现有配置、媒体库、任务、批次和浏览器哨兵的全部原始字节',
                files=len(health_baseline))
            check(dialog.preview['state'] == 'healthy' and dialog.launch_new_button.isEnabled(), '健康通过后 UI 才开放新版本启动')
            launch(previous=False)
            launch(previous=True)
            report['operation'] = background(lambda: manager.result(identifier))
            phase[0] = '制作实际启动失败的哈希正确测试包'
            broken_zip = directory / 'startup-failure.zip'
            broken_manifest = background(lambda: _broken_package(session['package'], broken_zip))
            failure_preview = ui_step(lambda: dialog.import_local(broken_zip), '导入故意启动失败的独立版本')
            check(failure_preview and failure_preview['build_id'] == broken_manifest['build_id'],
                '启动失败样本通过真实哈希检查，不能用文件哈希代替可执行性检查')
            dialog.deploy_confirm_check.setChecked(True)
            dialog.unsigned_confirm_check.setChecked(True)
            failure_deployed = ui_step(dialog.deploy_update, '独立部署故意启动失败版本')
            check(failure_deployed and failure_deployed['state'] == 'deployed', '失败版本部署在单独目录')
            dialog.execute_confirm_check.setChecked(True)
            failure_health = ui_step(dialog.check_health, '执行故意无效 PE 的启动检查', 150)
            check(failure_health and failure_health.get('passed') is False, '真实进程启动失败明确报告失败', health=failure_health)
            check(dialog.preview['state'] == 'health_failed' and not dialog.launch_new_button.isEnabled(),
                '失败版本保留诊断且禁止打开新版本')
            check(Path(failure_deployed['target_path']).is_dir(), '启动失败未删除失败版本')
            launch(previous=True)
            check(Path(session['old_release'], 'MediaFlow.exe').is_file(), '新版本启动失败后旧 EXE 仍保留并真实启动')
            report['failure_operation'] = background(lambda: manager.result(failure_preview['id']))
            session['failure_operation_id'] = failure_preview['id']
            _write_json(session_path, session)

        phase[0] = '核对旧应用、原数据和媒体'
        actual = background(lambda: {'release': _hash_tree(Path(session['old_release'])),
                                      'data': _hash_tree(Path(session['old_data']))})
        check(actual['release'] == session['baseline']['release'], '全部旧版发布文件 SHA-256 与验收前完全一致',
            files=len(actual['release']))
        check(actual['data'] == session['baseline']['data'], '原设置、历史、批次、任务快照、媒体和 BrowserProfile 文件 SHA-256 完全一致',
            files=len(actual['data']))
        check(background(lambda: history_count(Path(session['old_data']) / 'history.sqlite3', immutable=True)) == 2,
            '更新与回退后原媒体库历史可访问且记录数未改变', history_records=2)
        active_gaps = [gap['seconds'] for gap in gaps]
        report['ui_peak_gap_seconds'] = max(active_gaps, default=0)
        check(len(ticks) > 2 and report['ui_peak_gap_seconds'] < 2.0, '后台导入、校验、解压和实际启动期间 Qt 事件循环持续响应',
            heartbeat_ticks=len(ticks), peak_gap_seconds=report['ui_peak_gap_seconds'])
        report['passed'] = True
    except Exception as error:
        report.update(error=str(error), traceback=traceback.format_exc())
    finally:
        phase[0] = '验收收尾'
        if dialog is not None:
            dialog.shutdown()
            try:
                wait(lambda: dialog.is_closed, 100)
            except Exception as error:
                report['passed'] = False
                report['shutdown_error'] = str(error)
        for tree in list(owned_trees):
            try:
                tree.terminate()
                tree.close()
            except Exception as error:
                report['passed'] = False
                report.setdefault('cleanup_errors', []).append(str(error))
        if manager is not None:
            # Every entry was created by this manager in this test invocation.
            for process in manager._launched:
                if process.poll() is None:
                    tree = ProcessTree(process)
                    tree.terminate()
                    tree.close()
        timer.stop()
        faulthandler.cancel_dump_traceback_later()
        trace.close()
        report['duration_seconds'] = round(time.monotonic() - started, 3)
        report['ui_peak_gap_seconds'] = max((gap['seconds'] for gap in gaps), default=0)
        report['ui_largest_gaps'] = sorted(gaps, key=lambda value: value['seconds'], reverse=True)[:8]
        for name, value in environment_before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        save()
    return 0 if report['passed'] else 1
