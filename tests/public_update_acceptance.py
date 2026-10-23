"""Actual packaged client -> public signed channel -> deployment/GUI/rollback.

No injected CA, test key, network fixture, or altered updater implementation.
Only the installation context points at the explicitly supplied preserved old
release; the report identifies the real running updater version separately.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import ssl
import threading
import time
import traceback
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtWidgets import QMessageBox
from core.process_tree import ProcessTree
from core.update_channel import load_channel
from core.update_manager import UpdateManager, UpdateError
from core.update_manifest import validate_manifest, UpdateValidationError, canonical_bytes
from core.update_transport import fetch
from tests.update_acceptance import _hash_tree, _seed_data, _write_json, _read_json
from ui.update_dialog import UpdateDialog
from utils.build_info import runtime_identity


def run_public_update_acceptance(app, args):
    def option(name):
        return args[args.index(name)+1]
    directory = Path(option('--update-public-check')).absolute()
    old = Path(option('--update-old-release')).absolute()
    if directory.exists() or any(p.is_symlink() or p.is_junction() for p in (directory,*directory.parents)):
        return 2
    directory.mkdir(parents=True)
    report = {'passed':False,'scope':'real_public_https_packaged_client',
        'started_at':datetime.now(timezone.utc).isoformat(), 'runtime_identity':runtime_identity(),
        'checks':[], 'public_online_verified':False, 'launched_processes':[]}
    report_path = directory/'result.json'
    ticks=[]
    phase=['configuration']
    dialog=None
    trees=[]
    env_before={}
    timer=QTimer()
    timer.setInterval(25)
    timer.timeout.connect(lambda:ticks.append(time.monotonic()))
    timer.start()
    def save():
        _write_json(report_path,report)
    def check(condition,description,**detail):
        report['checks'].append({'passed':bool(condition),'description':description,**detail})
        save()
        if not condition:
            raise AssertionError(description)
    def wait(predicate,seconds=240):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            app.processEvents()
            QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            if predicate():
                return
            time.sleep(.005)
        raise TimeoutError('Public acceptance timeout: '+phase[0])
    def background(function,seconds=240):
        box={}
        done=threading.Event()
        def work():
            try: box['value']=function()
            except Exception as e: box['error']=e
            finally: done.set()
        threading.Thread(target=work,daemon=True,name='public-update-acceptance').start()
        wait(done.is_set,seconds)
        if 'error' in box: raise box['error']
        return box.get('value')
    def ui(action,name,seconds=240):
        phase[0]=name
        before=len(ticks)
        check(bool(action()),name+' requested')
        wait(lambda:not dialog.busy,seconds)
        result=dialog.last_result
        check(bool(result) and result.get('passed') is not False,name+' returned a real successful result')
        report.setdefault('background_steps',[]).append({'stage':name,'heartbeat_ticks':len(ticks)-before})
        return result
    try:
        identity=report['runtime_identity']
        check(identity['packaged'] and identity['app_version']=='2.1.0','Actual packaged 2.1.0 updater')
        channel=load_channel()
        check(channel['key_state']=='initialized' and bool(channel['trusted_keys']), 'Embedded formal trust is initialized')
        source=channel['source_url']
        check(source=='https://github.com/xlxs123/MediaFlow-Releases/releases/latest/download/manifest.json',
              'Actual created public release repository is the embedded channel')
        for name in ('APPDATA','LOCALAPPDATA'):
            env_before[name]=os.environ.get(name)
            os.environ[name]=str(directory/'private'/name)
        env_before['QT_QPA_PLATFORM']=os.environ.get('QT_QPA_PLATFORM')
        os.environ['QT_QPA_PLATFORM']='offscreen'
        for repo in ('MediaFlow','MediaFlow-Releases'):
            value=background(lambda repo=repo:json.loads(fetch('https://api.github.com/repos/xlxs123/'+repo,
                context=ssl.create_default_context(),total_timeout=40)))
            check(value.get('full_name')=='xlxs123/'+repo and value.get('private') is False,
                  'Repository accessible without authentication',repository=repo)
        current=UpdateManager(state_dir=directory/'current-channel-check',release_root=Path(identity['executable']).parent,
            current_identity=identity,current_data_dir=directory/'never-opened-current-data')
        phase[0]='public current-version check'
        actual=background(current.check_updates,90)
        manifest=actual['manifest']
        report['public_manifest']={'source':source,'version':manifest['version'],'build_id':manifest['build_id'],
            'key_id':manifest['signature']['key_id'],'package':manifest['package']}
        check(actual['status']=='up_to_date' and actual['authenticated'] is True,
              'Actual current client verifies and finds its published stable version',status=actual['status'])
        detached=background(lambda:json.loads(fetch(source.replace('manifest.json','manifest.sig'),
            context=ssl.create_default_context(),total_timeout=45)),60)
        check(detached.get('manifest_sha256')==hashlib.sha256(canonical_bytes(manifest)).hexdigest()
              and detached.get('signature')==manifest['signature'],'Public detached signature matches verified manifest')
        changed=deepcopy(manifest)
        changed['release_notes']+=' tampered'
        try: validate_manifest(changed,channel['trusted_keys'],require_signature=True)
        except UpdateValidationError: rejected=True
        else: rejected=False
        check(rejected,'Tampered remote manifest is rejected')
        old_identity=_read_json(old/'build-info.json')
        report['installation_context']={'version':old_identity['app_version'],'build_id':old_identity['build_id'],
            'note':'2.1.0 updater exercises preserved old installation context; old EXE is used for actual rollback, not claimed to contain this CDN adapter'}
        before_old=background(lambda:_hash_tree(old))
        data=directory/'preserved-old-data'
        background(lambda:_seed_data(data))
        before_data=background(lambda:_hash_tree(data))
        manager=UpdateManager(state_dir=directory/'updates',release_root=old,current_identity=old_identity,
            current_data_dir=data,health_data_root=directory/'health',launch_env=dict(os.environ,
                PYINSTALLER_RESET_ENVIRONMENT='1',QT_QPA_PLATFORM='offscreen'))
        dialog=UpdateDialog(manager)
        wait(lambda:not dialog.busy)
        check(not dialog.isVisible(),'Acceptance does not take over foreground UI')
        checked=ui(dialog.check_updates,'Public update discovery',90)
        check(checked['status']=='update_available' and checked['authenticated'],
              'Signed stable version compares newer than preserved installation')
        preview=ui(dialog.download_update,'Real public package download and validation',1800)
        check(preview['authenticated'] and preview['version']==identity['app_version'],
              'Downloaded ZIP identity and formal signature validated')
        cache=manager.state_dir/'downloads'/(preview['id']+'.zip')
        def digest_file(path):
            h=hashlib.sha256()
            with path.open('rb') as f:
                for block in iter(lambda:f.read(1024*1024),b''): h.update(block)
            return h.hexdigest()
        check(cache.stat().st_size==manifest['package']['size']
              and background(lambda:digest_file(cache))==manifest['package']['sha256'],
              'Public ZIP actual size and full SHA-256 match signed manifest')
        identifier=preview['id']
        phase[0]='tampered package rejection'
        with cache.open('r+b') as f:
            original=f.read(1); f.seek(0); f.write(bytes([original[0]^1])); f.flush(); os.fsync(f.fileno())
        try: background(lambda:manager.deploy(identifier,confirmed=True),120)
        except UpdateError: tampered_rejected=True
        else: tampered_rejected=False
        check(tampered_rejected and not Path(preview['target_path']).exists(),
              'Tampered real downloaded ZIP refused before extraction or deployment')
        # Restore this test-owned cache byte, then verify its full digest again.
        # The server, signature, manifest and updater validation stay unchanged.
        with cache.open('r+b') as f:
            f.write(original); f.flush(); os.fsync(f.fileno())
        check(background(lambda:digest_file(cache))==manifest['package']['sha256'],
              'Original downloaded package restored and full digest rechecked for positive deployment')
        check(not dialog.deploy_update(),'Deployment requires separate confirmation')
        dialog.deploy_confirm_check.setChecked(True)
        deployed=ui(dialog.deploy_update,'Independent version deployment',300)
        check(deployed['state']=='deployed' and Path(deployed['target_path'])!=old,'New and old releases are separate')
        health_data=directory/'health'/identity['app_version']
        background(lambda:_seed_data(health_data))
        health_before=background(lambda:_hash_tree(health_data))
        dialog.execute_confirm_check.setChecked(True)
        health=ui(dialog.check_health,'Actual new EXE startup health',150)
        check(health.get('passed') is True and health['identity']['build_id']==identity['build_id'],
              'Deployed EXE actually runs and reports expected build')
        check(background(lambda:_hash_tree(health_data))==health_before,'Health check preserves real private database/task/profile bytes')
        def launch(previous=False):
            version=old_identity['app_version'] if previous else identity['app_version']
            roaming=directory/'private'/('rollback' if previous else 'new-gui')/'Roaming'
            local=roaming.parent/'Local'
            target=roaming/'MediaFlow'/version
            target.mkdir(parents=True)
            _write_json(target/'migration-assistant.json',{'version':1,'handled':True,'result':'private public acceptance'})
            manager.launch_env.update(APPDATA=str(roaming),LOCALAPPDATA=str(local))
            with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):
                outcome=ui(dialog.launch_previous if previous else dialog.launch_new,
                    'Actual old GUI rollback' if previous else 'Actual new GUI launch',90)
            process=manager._launched[-1]
            tree=ProcessTree(process); trees.append(tree)
            db=target/'history.sqlite3'
            wait(lambda:process.poll() is not None or db.exists(),90)
            check(process.poll() is None and db.is_file(),'Actual GUI stays running and initializes private media library',version=version)
            import sqlite3
            def read_database():
                with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as con:
                    return con.execute('SELECT count(*) FROM tasks').fetchone()[0]
            check(background(read_database)>=0,'Launched GUI media library is readable',version=version)
            report['launched_processes'].append({'version':version,'pid':process.pid,'path':outcome['path'],'library_readable':True})
            tree.terminate(); tree.close(); trees.remove(tree)
        launch()
        repeated=background(lambda:manager.deploy(identifier,confirmed=True),120)
        check(repeated['target_path']==deployed['target_path'],
              'Repeated same signed operation revalidates existing deployment without overwrite')
        launch(previous=True)
        check(background(lambda:_hash_tree(old))==before_old,'Preserved old release is byte-for-byte unchanged')
        check(background(lambda:_hash_tree(data))==before_data,'Old database/history/tasks/media/profile fixture bytes unchanged')
        reopened=UpdateManager(state_dir=manager.state_dir,release_root=old,current_identity=old_identity,
            current_data_dir=data,health_data_root=directory/'health',launch_env=manager.launch_env)
        result=background(lambda:reopened.result(identifier))
        check(result['version']==identity['app_version'] and result['build_id']==identity['build_id'],
              'Restarted update manager recovers actual deployed version state')
        report['operation']=result
        report.update(passed=True,public_online_verified=True)
        return 0
    except Exception as error:
        report.update(error=str(error),error_type=type(error).__name__,phase=phase[0],traceback=traceback.format_exc())
        return 1
    finally:
        timer.stop()
        for tree in trees:
            tree.terminate(); tree.close()
        if dialog is not None:
            dialog.close(); dialog.deleteLater()
        for name,value in env_before.items():
            if value is None: os.environ.pop(name,None)
            else: os.environ[name]=value
        report['ui_maximum_gap_seconds']=max((b-a for a,b in zip(ticks,ticks[1:])),default=0)
        report['heartbeat_ticks']=len(ticks)
        report['ended_at']=datetime.now(timezone.utc).isoformat()
        save()
