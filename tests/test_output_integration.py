import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from copy import deepcopy
from dataclasses import fields
from datetime import datetime
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
from core.batch_controller import BatchController
from core.batch_store import BatchStore
from core.download_queue import DownloadQueue
from core.downloader import DownloadRequest, build_arguments
from core.media_groups import group_media
from core.output_organization import make_values
from core.output_relocation import apply_completed_paths, update_workspace_paths
from core.recovery_workspace import RecoveryWorkspace, file_hash
from core.task_store import TaskStore, _canonical
from tests.test_batch_controller import SerialDriver, media
from tests.test_download_queue import WorkerFactory
from utils.config import ConfigStore, DEFAULTS


class OutputIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queues = []
        self.controllers = []

    def tearDown(self):
        for controller in self.controllers:
            controller.shutdown()
        for queue in self.queues:
            for worker in tuple(queue._workers):
                worker.finish(success=False, cancelled=True)
        for controller in self.controllers:
            self.wait(lambda: controller.is_closed)
        self.app.processEvents()
        self.temp.cleanup()

    def wait(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.002)
        self.fail('后台操作超时')

    def queue(self):
        queue = DownloadQueue(worker_factory=WorkerFactory(), task_store=TaskStore(self.root/'tasks.json'))
        self.queues.append(queue)
        return queue

    def request(self, **changes):
        values = dict(yt_dlp_path='yt-dlp.exe', url='https://media.example/video.mp4',
            output_directory=str(self.root/'downloads'), filename='原始标题', output_extension='mp4',
            organization={'filename_template':'{site}_{title}_{resolution}.{ext}', 'classification':'type_site'},
            media_metadata={'organization_values':make_values('中文<>标题', site='Douyin', resolution=(1920,1080))})
        values.update(changes)
        return DownloadRequest(**values)

    def test_output_is_classified_but_temp_remains_under_original_root(self):
        queue = self.queue()
        job = queue.add(self.request())
        self.assertEqual(Path(job.output_path).parent, self.root/'downloads'/'视频'/'Douyin')
        self.assertIn('Douyin_中文_标题_1080p.mp4', job.output_path)
        self.assertEqual(Path(job.request.temporary_directory).parent, self.root/'downloads'/'.stream-downloads')
        self.assertFalse(Path(job.output_path).parent.exists())
        build_arguments(job.request, require_tools=False)

    def test_direct_url_can_use_actual_host_without_inventing_media_id(self):
        job=self.queue().add(self.request(media_metadata={},organization={
            'filename_template':'{site}_{title}_{id}.{ext}','classification':'site'}))
        self.assertEqual(Path(job.output_path).parent.name,'media.example')
        self.assertIn('未知ID',Path(job.output_path).name)

    def test_conflicts_on_disk_and_in_queue_never_overwrite(self):
        queue = self.queue()
        first = queue.add(self.request())
        Path(first.output_path).parent.mkdir(parents=True)
        Path(first.output_path).write_bytes(b'existing normal file')
        second, third = queue.add(self.request()), queue.add(self.request())
        self.assertEqual(len({first.output_path,second.output_path,third.output_path}),3)
        self.assertEqual(Path(first.output_path).read_bytes(), b'existing normal file')

    def test_resume_uses_original_naming_snapshot_after_settings_change(self):
        queue = self.queue()
        job = queue.add(self.request())
        job.request.resume_identity = {'layout_sha256':'same-media'}
        queue.save_tasks()
        restored = self.queue().restore_tasks()[0]
        self.assertEqual(restored.output_path,job.output_path)
        self.assertEqual(restored.request.temporary_directory,job.request.temporary_directory)
        self.assertEqual(restored.state,'paused')
        self.assertEqual(restored.request.media_metadata['organization_values'],job.request.media_metadata['organization_values'])

    def test_legacy_request_missing_new_fields_still_restores(self):
        queue=self.queue();job=queue.add(self.request(organization={}))
        queue.save_tasks()
        # Build a real v1 encrypted request without any new dataclass fields.
        document=json.loads(queue.task_store.path.read_text('utf-8'))
        import base64,hashlib
        request=json.loads(TaskStore._unprotect(base64.b64decode(document['tasks'][0]['request_dpapi'])))
        for key in ('organization','final_output_directory','collection_directory','workspace_output_directory'):
            request.pop(key,None)
            document['tasks'][0]['metadata']['request'].pop(key,None)
        document['tasks'][0]['request_dpapi']=base64.b64encode(TaskStore._protect(_canonical(request))).decode()
        document.pop('checksum')
        document['checksum']=hashlib.sha256(_canonical(document)).hexdigest()
        queue.task_store.path.write_text(json.dumps(document),encoding='utf-8')
        restored=self.queue().restore_tasks()[0]
        self.assertFalse(restored.context_error)
        self.assertEqual(restored.output_path,job.output_path)

    def test_media_and_subtitle_submit_as_associated_final_names(self):
        job=self.queue().add(self.request())
        workspace=RecoveryWorkspace(job.request,{'layout_sha256':'local'},'Direct')
        workspace.prepare()
        media_path=workspace.home/'base.mp4';media_path.write_bytes(b'unit-media')
        subtitle=workspace.home/'en.vtt';subtitle.write_text('WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello',encoding='utf-8')
        validations=[{}]
        output=workspace.publish([media_path],validations,[{'path':str(subtitle),'id':'abcdefgh1234','format':'vtt','language':'en'}])
        self.assertEqual(output,[job.output_path])
        sidecar=Path(validations[0]['subtitle_files'][0]['path'])
        self.assertEqual(sidecar.parent,Path(job.output_path).parent)
        self.assertTrue(sidecar.name.startswith(Path(job.output_path).stem+'.en.'))
        self.assertFalse(media_path.exists())

    def test_directory_failure_keeps_staged_media(self):
        job=self.queue().add(self.request())
        workspace=RecoveryWorkspace(job.request,{'layout_sha256':'local'},'Direct');workspace.prepare()
        source=workspace.home/'base.mp4';source.write_bytes(b'keep')
        obstacle=self.root/'downloads'/'视频';obstacle.write_text('not a directory')
        with self.assertRaises(Exception) as captured:
            workspace.publish([source],[{}])
        self.assertIn('最终分类目录',str(captured.exception))
        self.assertEqual(source.read_bytes(),b'keep')

    def test_library_relocation_updates_task_and_workspace_without_losing_identity(self):
        queue=self.queue();job=queue.add(self.request());job.request.resume_identity={'layout_sha256':'local'}
        workspace=RecoveryWorkspace(job.request,job.request.resume_identity,'Direct');workspace.prepare()
        source=workspace.home/'base.mp4';source.write_bytes(b'owned-media')
        workspace.publish([source],[{}])
        old=job.output_path;temporary=job.request.temporary_directory
        job.state='completed';job.result={'success':True,'files':[old],'output_validation':[{'path':old}]}
        target=self.root/'organized'/'改名.mp4';target.parent.mkdir();Path(old).rename(target)
        affected=apply_completed_paths(queue,[{'old':old,'new':str(target),'kind':'media'}])
        self.assertEqual(affected,[job]);self.assertEqual(job.request.temporary_directory,temporary)
        self.assertTrue(update_workspace_paths(job.request))
        updated=RecoveryWorkspace(job.request,job.request.resume_identity,'Direct')
        self.assertEqual(updated.committed_files(),[str(target)])
        restored=self.queue().restore_tasks()[0]
        self.assertFalse(restored.context_error)
        self.assertEqual(restored.result['files'],[str(target)])
        self.assertEqual(restored.state,'completed')

    def test_old_log_cannot_retarget_a_new_job_reusing_the_old_name(self):
        queue=self.queue();job=queue.add(self.request());job.state='completed';job.result={'files':[job.output_path]}
        old=job.output_path
        self.assertFalse(apply_completed_paths(queue,[{'old':old,'new':str(self.root/'elsewhere.mp4'),'task_key':'different-task'}]))
        self.assertEqual(job.output_path,old)

    def test_version_config_does_not_mutate_previous_user_data(self):
        with patch.dict(os.environ,{'APPDATA':str(self.root)}):
            old=self.root/'MediaFlow'/'config.json';old.parent.mkdir();old.write_text('{"output_directory":"G:/my-media"}')
            before=old.read_bytes();store=ConfigStore();values=store.load()
            from utils.branding import APP_VERSION
            self.assertEqual(store.path.parent,self.root/'MediaFlow'/APP_VERSION)
            self.assertEqual(values['output_directory'],DEFAULTS['output_directory'])
            values['organization']={'filename_template':'{title}.{ext}','classification':'site'}
            store.save(values)
            self.assertEqual(old.read_bytes(),before)
            self.assertEqual(store.load()['organization']['classification'],'site')

    def test_legacy_completed_direct_without_preallocated_path_keeps_actual_file_after_organization(self):
        queue=self.queue();job=queue.add(self.request(organization={},output_extension=''))
        self.assertFalse(job.output_path)
        old=Path(job.request.output_directory)/(job.name+'.mp4');old.parent.mkdir(parents=True,exist_ok=True)
        old.write_bytes(b'legacy-final-media')
        job.state='completed';job.result={'success':True,'files':[str(old)]}
        target=self.root/'organized'/'legacy.mp4';target.parent.mkdir();old.rename(target)
        self.assertEqual(apply_completed_paths(queue,[{'old':str(old),'new':str(target),'kind':'media'}]),[job])
        self.assertEqual(job.output_path,str(target))
        restored=self.queue().restore_tasks()[0]
        self.assertFalse(restored.context_error)
        self.assertEqual(restored.result['files'],[str(target)])

    def test_collection_restarts_with_same_folder_and_completed_item(self):
        queue=self.queue();driver=SerialDriver()
        controller=BatchController(queue,driver,self.root/'batches.sqlite',lambda options:self.request(output_directory=options['output_directory'],organization={}))
        self.controllers.append(controller);self.wait(lambda:controller.ready)
        options={'output_directory':str(self.root/'downloads'),'collection_name':'我的合集',
                 'organization':{'filename_template':'{title}.{ext}','classification':'site'}}
        batch_id=controller.create(['https://pages.example.test/first','https://pages.example.test/second'],options)
        self.wait(lambda:driver.busy)
        collection=controller.batches[batch_id]['options']['collection_directory']
        driver.finish(group_media([media('first')]))
        self.wait(lambda:len(queue.jobs)==1)
        job=queue.jobs[0];self.assertTrue(Path(job.output_path).is_relative_to(Path(collection)))
        controller.batch_action('pause',batch_id)
        self.wait(lambda:job.worker is None or job.worker.stop_requested)
        if job.worker:
            job.worker.finish(success=False,cancelled=True)
            self.app.processEvents()
        # Durable completed item must remain terminal on reload, without re-navigation.
        item=next(i for i in controller.items.values() if i.get('job_id')==job.id)
        job.state='completed';job.result={'success':True,'files':[job.output_path]};queue.save_tasks()
        controller._sync_job(item,job);controller._touch(item,True)
        controller.shutdown();self.wait(lambda:controller.is_closed)
        restored_queue=self.queue();restored_queue.restore_tasks();restored_driver=SerialDriver()
        restored=BatchController(restored_queue,restored_driver,self.root/'batches.sqlite',controller.template_factory)
        self.controllers.append(restored);self.wait(lambda:restored.ready)
        self.assertEqual(restored.batches[batch_id]['options']['collection_directory'],collection)
        self.assertEqual(restored.items[item['id']]['state'],'completed')
        self.assertFalse(restored_driver.started)
        # 完成队列项已移除后，仍可由持久task_key更新合集中的真实路径。
        old_path=restored.items[item['id']]['output_path']
        target=str(self.root/'organized'/'collection-renamed.mp4')
        restored_queue.jobs.clear()
        self.assertEqual(restored.relocate_completed_paths([{'old':old_path,'new':target,'kind':'media',
            'task_key':restored.items[item['id']]['queue_key']}]),1)
        self.assertEqual(restored.items[item['id']]['output_path'],target)

