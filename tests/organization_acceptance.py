"""1.6正式程序本地验收：真实下载、输出轨道、文件整理和跨进程恢复。"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import threading
import time
import traceback

from PySide6.QtCore import QCoreApplication, QEvent, QTimer

from core.downloader import DownloadRequest
from core.library_validation import revalidate_media
from core.media_history import HistoryStore
from core.output_organization import directory_for, make_values, render_filename
from core.recovery_workspace import file_hash
from core.tool_manager import APP_DIR
from tests.track_cases import request_for
from tests.track_fixture import TrackFixture
from ui.batch_import import BatchImportDialog
from ui.main_window import MainWindow
from ui.organize_dialog import OrganizeDialog
from utils.build_info import runtime_identity
from utils.config import ConfigStore


def run_organization_acceptance(app, args):
    directory = Path(args[args.index('--organization-check') + 1]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    restart = '--organization-restart' in args
    result_path = directory / ('restart-result.json' if restart else 'result.json')
    report = {'passed':False,'restart':restart,'runtime_identity':runtime_identity(),
              'checks':[],'outputs':[],'external_downloads':False}
    window = fixture = None
    ticks = []
    phase = ['startup']
    gaps = []
    timer = QTimer();timer.setInterval(25)
    def beat():
        now=time.monotonic()
        if ticks:
            gaps.append({'seconds':now-ticks[-1],'phase':phase[0]})
        ticks.append(now)
    timer.timeout.connect(beat);timer.start()
    app.setQuitOnLastWindowClosed(False)
    def save():
        result_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def check(value, label, **evidence):
        report['checks'].append({'label':label,'passed':bool(value),**evidence});save()
        if not value:
            raise AssertionError(label + ': ' + repr(evidence))
    def wait(predicate, timeout=90):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            app.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            if predicate():return
            time.sleep(.004)
        raise TimeoutError('本地验收后台步骤超时：'+phase[0])
    def background(function,timeout=120):
        state={};done=threading.Event()
        def run():
            try:state['value']=function()
            except Exception as error:state['error']=error
            finally:done.set()
        threading.Thread(target=run,daemon=True).start();wait(done.is_set,timeout)
        if state.get('error'):raise state['error']
        return state.get('value')
    def history():
        with HistoryStore(directory/'history.sqlite3') as store:
            return store.query(view='history',limit=100)['items']
    def validate(path):
        return revalidate_media(path,ffmpeg_path=str(APP_DIR/'tools/ffmpeg.exe'),
            ffprobe_path=str(APP_DIR/'tools/ffprobe.exe'),timeout=120)
    def output(request,label):
        phase[0]='download:'+label
        window.queue.pause();job=window.queue.add(request);window.queue.start()
        wait(lambda:job.worker is None and job.state in ('completed','failed','cancelled','waiting_context'),120)
        check(job.state=='completed','真实队列下载完成',case=label,error=(job.result or {}).get('error',''))
        phase[0]='verify:'+label
        actual_path=job.output_path or job.result['files'][0]
        actual=background(lambda:validate(actual_path))
        check(actual['validation_state']=='valid','ffprobe与完整轨道读取通过',case=label,reason=actual['validation_reason'])
        check(Path(job.request.temporary_directory).parent==Path(request.output_directory)/'.stream-downloads',
              '临时工作区与最终分类目录分离',case=label)
        report['outputs'].append({'case':label,'job_id':job.id,'path':actual_path,
            'sha256':background(lambda:file_hash(actual_path)),'validation':actual['validation']})
        return job
    try:
        config=ConfigStore(directory/'config.json')
        if not restart:config.save({'output_directory':str(directory/'downloads')})
        window=MainWindow(config);window.showMinimized()
        wait(lambda:window.detector is None and window.batch_controller.ready and window.yt_info.found
             and window.ff_info.found and window.probe_info.found,50)
        check(True,'程序自动定位并验证三个工具',tools=[window.yt_info.version,window.ff_info.version,window.probe_info.version])
        check(all(Path(info.path).parent==APP_DIR/'tools' for info in (window.yt_info,window.ff_info,window.probe_info)),
              '三个工具均从实际程序位置定位，与启动工作目录无关',paths=[window.yt_info.path,window.ff_info.path,window.probe_info.path])
        if restart:
            first=json.loads((directory/'result.json').read_text('utf-8'))
            check(first['passed'],'上一进程完整验收通过')
            before=len(window.queue.jobs)
            for item in first['outputs']:
                check(background(lambda item=item:file_hash(item['path']))==item['sha256'],
                      '重启后实际媒体路径和内容保持一致',case=item['case'])
            check(len(window.queue.jobs)==before and not any(job.worker for job in window.queue.jobs),
                  '重启不重复下载已完成任务',jobs=before)
            rows=background(history)
            check(set(item['path'] for item in first['outputs']) <= {row['path'] for row in rows},
                  '媒体库持久化实际整理后路径')
            for batch_id, folder in first['collections'].items():
                check(window.batch_controller.batches[batch_id]['options']['collection_directory']==folder,
                      '合集恢复使用原目录',batch=batch_id)
            check(window.output_options.options()==first['settings'],'命名分类设置在重启后恢复')
            report['outputs']=first['outputs']
        else:
            fixture=TrackFixture(directory/'fixture',tools=APP_DIR/'tools')
            background(fixture.generate);fixture.start()
            window.filename.setText('中文:标题 / 带特殊字符')
            window.output_options.template_edit.setText('{site}_{title}_{resolution}_{date}.{ext}')
            window.output_options.classification_combo.setCurrentIndex(window.output_options.classification_combo.findData('type_site'))
            window._update_output_preview();window.save_settings()
            check('中文' in window.output_options.preview.text() and window.output_options.preview.text().endswith('.mp4'),
                  '设置中实时预览保留中文并清理非法字符',preview=window.output_options.preview.text())
            report['settings']=window.output_options.options()
            requests=[]
            for kind,prefs in (('Direct',None),('HLS',{'audio_language':'ja'}),
                               ('DASH',{'audio_language':'ja','subtitle_mode':'sidecar','subtitle_languages':['en'],'container':'mkv'})):
                request,_=background(lambda kind=kind,prefs=prefs:request_for(fixture,directory/'downloads',kind,prefs))
                request.organization=deepcopy(report['settings'])
                request.media_metadata['organization_values']=make_values('中文:'+kind+' / 标题',
                    source_url=fixture.base+'page/'+kind.lower(),resolution=(320,180),extension=request.output_extension)
                requests.append(request)
            planned=directory_for(requests[0].output_directory,requests[0].organization,requests[0].media_metadata['organization_values'])
            original=planned/render_filename(requests[0].organization['filename_template'],
                requests[0].media_metadata['organization_values'],'mp4',planned)
            original.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(fixture.directory/'single.mp4',original)
            original_hash=background(lambda:file_hash(original))
            first=output(requests[0],'Direct中文与冲突')
            second=output(deepcopy(requests[0]),'Direct重复文件续号')
            check(len({str(original),first.output_path,second.output_path})==3 and file_hash(original)==original_hash,
                  '磁盘与队列同名冲突均未覆盖已有正常MP4')
            requests[1].organization={'filename_template':'{title}_{time}.{ext}','classification':'date'}
            hls=output(requests[1],'HLS日期分类')
            dash=output(requests[2],'DASH日语音轨与独立字幕MKV')
            check(Path(dash.output_path).suffix=='.mkv','最终扩展名与实际封装容器一致')
            subtitles=dash.result['output_validation'][0].get('subtitle_files',[])
            check(len(subtitles)==1 and Path(subtitles[0]['path']).parent==Path(dash.output_path).parent
                  and Path(subtitles[0]['path']).name.startswith(Path(dash.output_path).stem+'.'),
                  '独立字幕与媒体按同一stem关联提交')
            audio=DownloadRequest(str(APP_DIR/'tools/yt-dlp.exe'),fixture.base+'en.m4a',str(directory/'downloads'),
                filename='本地音频',output_extension='m4a',ffmpeg_path=str(APP_DIR/'tools/ffmpeg.exe'),
                ffprobe_path=str(APP_DIR/'tools/ffprobe.exe'),organization={'filename_template':'{title}.{ext}','classification':'type'},
                media_metadata={'organization_values':make_values('本地音频',source_url=fixture.base,extension='m4a')})
            audio_job=output(audio,'Direct音频分类')
            check(Path(audio_job.output_path).parent.name=='音频','音频分类使用真实输出类型')
            legacy_request=DownloadRequest(str(APP_DIR/'tools/yt-dlp.exe'),fixture.base+'single.mp4',
                str(directory/'downloads/legacy'),filename='旧版手动入口',
                ffmpeg_path=str(APP_DIR/'tools/ffmpeg.exe'),ffprobe_path=str(APP_DIR/'tools/ffprobe.exe'))
            legacy=output(legacy_request,'旧版未预分配路径的Direct兼容')
            legacy_path=legacy.result['files'][0]
            check(not legacy.output_path,'旧式任务保持原有未预分配路径行为直到显式整理')
            phase[0]='history'
            wait(lambda:len(background(history))>=6,30)
            # 暂停导航，仅验收导入、独占合集目录和状态持久化；真正CDP发现由tracks-organization另行实测。
            window.batch_driver.can_acquire=lambda:False
            controller=window.batch_controller
            existing=directory/'downloads'/'我的合集';existing.mkdir();(existing/'user-file.txt').write_text('keep',encoding='utf-8')
            imported=BatchImportDialog(window,defaults={'output_directory':str(directory/'downloads')})
            imported.collection_name.setText('我的合集')
            imported.editor.setPlainText(fixture.base+'single.mp4?item=one\n'+fixture.base+'single.mp4?item=two')
            imported.refresh_preview();imported.accept()
            check(len(imported.result_urls)==2 and imported.options['collection_name']=='我的合集','实际批量导入界面提交合集和完整查询参数')
            batch=window.submit_batch_urls(imported.result_urls,imported.options);imported.deleteLater()
            second_batch=controller.create([fixture.base+'single.mp4?item=three'],dict(imported.options))
            wait(lambda:not controller._pending_creates)
            folders={key:controller.batches[key]['options']['collection_directory'] for key in (batch,second_batch)}
            check(len(set(folders.values())|{str(existing)})==3 and (existing/'user-file.txt').read_text('utf-8')=='keep',
                  '新合集不覆盖已有合集或另一个新合集',folders=list(folders.values()))
            controller.batch_action('pause',batch);controller.batch_action('resume',batch);controller.batch_action('pause',batch)
            controller.batch_action('cancel',second_batch)
            check(all(item['state']=='paused' for item in controller.items.values() if item['batch_id']==batch),
                  '整批暂停和继续保留独立任务状态')
            report['collections']=folders
            phase[0]='organize'
            rows=background(history);selected=[row for row in rows if row['path'] in (first.output_path,dash.output_path,legacy_path)]
            check(len(selected)==3,'新任务及旧式Direct任务均自动进入媒体库')
            results={}
            window.library_service.action_ready.connect(lambda command,value:results.__setitem__(command,value))
            original_paths=[row['path'] for row in selected]
            organizer=OrganizeDialog(window.library_service,selected,window)
            organizer.root_edit.setText(str(directory/'organized'))
            organizer.classification.setCurrentIndex(organizer.classification.findData('site'))
            organizer.filename_template.setText('{title}_{resolution}.{ext}')
            organizer.showMinimized();organizer.request_preview()
            wait(lambda:'organize_preview' in results and organizer.plan is not None)
            preview=results['organize_preview'];report['organize_preview']=preview
            check(preview['ready_count']==3 and all(Path(path).is_file() for path in original_paths),
                  '后台整理预览展示原路径目标冲突和目录，尚未移动文件',preview=preview)
            check(not organizer.apply_button.isEnabled(),'实际整理窗口在勾选确认前禁止执行')
            organizer.grab().save(str(directory/'organization-preview.png'))
            window.library_service.apply_organization(preview['token'],confirmed=False)
            wait(lambda:'organize_apply' in results)
            check(bool(results.pop('organize_apply').get('error')) and all(Path(path).is_file() for path in original_paths),
                  '未经确认不能执行整理')
            organizer.confirm_check.setChecked(True)
            check(organizer.apply_button.isEnabled(),'检查路径后可从实际确认按钮执行')
            organizer.apply_button.click()
            wait(lambda:'organize_apply' in results)
            applied=results['organize_apply'];report['organize_result']=applied
            check(applied['completed']==3 and applied['failed']==0,'确认后三个媒体及已登记字幕安全移动',result=applied)
            check(bool(legacy.output_path) and Path(legacy.output_path).is_file(),'旧式Direct任务整理后队列恢复路径同步')
            wait(lambda:not window._relocation_workers)
            check(all(not Path(path).exists() for path in original_paths),'整理执行移动，原路径不留副本')
            check(Path(audio_job.output_path).is_file() and Path(second.output_path).is_file(),'未选文件未被整理')
            mapping={item['old']:item['new'] for item in applied['mappings']}
            for record in report['outputs']:
                record['path']=mapping.get(record['path'],record['path'])
                check(background(lambda record=record:file_hash(record['path']))==record['sha256'],
                      '移动和重命名保持媒体字节不变',case=record['case'])
            updated=background(history)
            check(set(record['path'] for record in report['outputs']) <= {row['path'] for row in updated},
                  '数据库、任务队列与最终磁盘路径一致')
            check(all(Path(item['new']).is_file() and not Path(item['old']).exists() for item in applied['mappings']),
                  '关联字幕随视频移动，不保留过时路径')
            organizer.reject();organizer.deleteLater()
            window.settings_toggle.setChecked(True)
            window.input_tabs.setCurrentIndex(0)
            window.settings_scroll.verticalScrollBar().setValue(window.settings_scroll.verticalScrollBar().maximum())
            app.processEvents()
            window.grab().save(str(directory/'organization-settings.png'))
            window.settings_toggle.setChecked(False)
            window.input_tabs.setCurrentWidget(window.media_library);window.media_library.set_active(True)
            wait(lambda:not window.media_library._loading if hasattr(window.media_library,'_loading') else True,5)
            app.processEvents();window.grab().save(str(directory/'organization-library.png'))
        report['passed']=True
    except Exception:
        report['error']=traceback.format_exc()
    finally:
        timer.stop()
        report['ui_peak_seconds']=max((gap['seconds'] for gap in gaps),default=0)
        report['ui_gaps']=sorted(gaps,key=lambda gap:gap['seconds'],reverse=True)[:8]
        if window:
            try:
                window.close();wait(lambda:window.library_service.is_closed and window.batch_controller.is_closed
                                    and not window._relocation_workers and not window.queue.active_count,60)
                window.queue.save_tasks()
            except Exception:
                report['close_error']=traceback.format_exc();report['passed']=False
        if fixture:fixture.close()
        save()
    return 0 if report['passed'] else 1
