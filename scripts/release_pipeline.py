"""Local guarded orchestration; delegates signing, deployment and ZIP validation.

DryRun signs a private rehearsal package and reads GitHub; it never creates tags,
drafts, uploads or publishes. Existing-public verification is a separate result.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from core.process_tree import ProcessTree, NO_WINDOW
from core.data_migration import frozen_file
from core.update_channel import read_channel, key_fingerprint
from core.update_manifest import parse_version, safe_release_path
from scripts.package_update import _plain, _read_stamp
from scripts.publish_release import _file_hash, _read_document, _private_key, _trusted_keys
from scripts.publish_github_release import prepare_assets, verified_asset_urls
from scripts.verify_hosted_release import _match_release_inventory
from scripts.security_gate import verify_security_run, verify_ci_provenance
from scripts.release_security import (authenticode, production_options, signing_derivation,
                                     ReleaseSecurityError)
from utils.branding import APP_VERSION
from utils.build_info import source_digest


class PipelineError(RuntimeError):
    """Messages are fixed operator guidance, never raw command/credential output."""


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temporary.open('x',encoding='utf-8') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)


def document(path, *, maximum=2*1024*1024):
    return _read_document(path,maximum)[0]


def safe_environment(env=None):
    result=dict(os.environ if env is None else env)
    for name in tuple(result):
        upper=name.upper()
        if upper in ('GH_TOKEN','GITHUB_TOKEN','MEDIAFLOW_PROTECTED_PUBLISHER_PASSWORD') or (
                upper.startswith('MEDIAFLOW_') and any(word in upper for word in ('PASSWORD','PRIVATE_KEY','SECRET'))):
            result.pop(name,None)
    result.update(PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1',QT_QPA_PLATFORM='offscreen',
                  GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='Never',GH_PROMPT_DISABLED='1',GH_NO_UPDATE_NOTIFIER='1')
    return result


class Runner:
    def __init__(self,root,directory):
        self.root,self.directory=Path(root),Path(directory)

    def call(self,args,stage,*,timeout=600,env=None,logs=True,input_bytes=None,allow_failure=False):
        process=tree=None
        try:
            process=subprocess.Popen([str(a) for a in args],cwd=self.root,
                stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                env=safe_environment() if env is None else env,creationflags=NO_WINDOW)
            tree=ProcessTree(process)
            try:
                output,error=process.communicate(input=input_bytes,timeout=timeout)
            except subprocess.TimeoutExpired as expired:
                tree.terminate()
                try:
                    output,error=process.communicate(timeout=15)
                except subprocess.TimeoutExpired as stopped:
                    output,error=stopped.output or expired.output or b'',stopped.stderr or expired.stderr or b''
                if logs:
                    for suffix,data in (('stdout',output),('stderr',error)):
                        with (self.directory/(stage+'.'+suffix+'.log')).open('xb') as handle:handle.write(data)
                raise PipelineError('阶段超时，已停止本次进程树：'+stage) from None
            if logs:
                # Authentication and vault operations always use logs=False.
                for suffix,data in (('stdout',output),('stderr',error)):
                    with (self.directory/(stage+'.'+suffix+'.log')).open('xb') as handle:handle.write(data)
            if process.returncode and not allow_failure:
                raise PipelineError('阶段失败：'+stage+'；退出码 '+str(process.returncode)+'。请查看本次阶段报告。')
            return process.returncode,output,error
        except OSError:
            raise PipelineError('无法启动阶段：'+stage+'。请检查工具、权限和路径。') from None
        finally:
            if tree:tree.close()


class GitHub:
    """Only fixed read endpoints here. All release writes use the existing publisher."""
    def __init__(self,runner,gh,git):
        self.runner,self.gh,self.git=runner,gh,git
        self.env=safe_environment()
        supplied={k:os.environ[k] for k in ('GH_TOKEN','GITHUB_TOKEN') if os.environ.get(k)}
        self.env.update(supplied)
        code,_,_=runner.call([gh,'auth','status','--hostname','github.com'],'github-auth',
            timeout=30,logs=False,allow_failure=True,env=self.env)
        self.auth_method='github-cli'
        if code:
            code,value,_=runner.call([git,'credential','fill'],'git-credential-helper',timeout=30,
                logs=False,input_bytes=b'protocol=https\nhost=github.com\n\n',allow_failure=True,env=self.env)
            credential=dict(line.split('=',1) for line in value.decode('utf-8').splitlines() if '=' in line)
            if code or not credential.get('password'):
                raise PipelineError('没有可用的 GitHub CLI 或 Git Credential Manager 授权；请仅完成一次正常登录。')
            self.env['GH_TOKEN']=credential.pop('password')
            credential.clear();value=b''
            self.auth_method='git-credential-manager'

    def api(self,path,*,missing=False):
        if not re.fullmatch(r'[A-Za-z0-9_./?=&%+@-]+',path) or path.startswith('/') or '..' in path:
            raise PipelineError('内部 GitHub API 路径无效。')
        code,value,_=self.runner.call([self.gh,'api','--method','GET',path],'github-read',
            timeout=90,env=self.env,logs=False,allow_failure=True)
        try:data=json.loads(value)
        except (ValueError,UnicodeError):raise PipelineError('GitHub 未返回有效 JSON；未执行发布写操作。') from None
        if code:
            if missing and isinstance(data,dict) and str(data.get('status'))=='404':return None
            raise PipelineError('GitHub 只读检查失败；请检查网络、权限或 API 限额。')
        return data

    def repo(self,name):
        return self.api('repos/'+name)

    def release(self,name,version):
        return self.api('repos/'+name+'/releases/tags/v'+version,missing=True)

    def security(self,repo,commit,*,default_branch,wait_seconds=1200):
        return verify_security_run(self,repo,commit,default_branch=default_branch,wait_seconds=wait_seconds)

    def ci(self,repo,commit,version,*,run_id='',wait_seconds=1200):
        if run_id and not re.fullmatch(r'[0-9]{1,20}',run_id):raise PipelineError('CI run ID 必须是实际的数字标识。')
        deadline=time.monotonic()+wait_seconds
        while True:
            if run_id:
                run=self.api('repos/'+repo+'/actions/runs/'+run_id)
                candidates=[run]
            else:
                candidates=self.api('repos/'+repo+'/actions/workflows/ci.yml/runs?head_sha='+commit+'&per_page=30')['workflow_runs']
            eligible=[r for r in candidates if r.get('head_sha')==commit
                and r.get('head_repository',{}).get('full_name','').casefold()==repo.casefold()
                and r.get('event') in ('push','workflow_dispatch')
                and r.get('path','').split('@')[0]=='.github/workflows/ci.yml']
            passed=next((r for r in eligible if r.get('status')=='completed' and r.get('conclusion')=='success'),None)
            if passed:break
            pending=any(r.get('status')!='completed' for r in eligible)
            if not pending or time.monotonic()>=deadline:
                raise PipelineError('当前源码提交没有通过所需 Windows CI。请提交并推送代码，等待该提交的 CI 通过后重试。')
            time.sleep(min(10,max(.1,deadline-time.monotonic())))
        identifier=str(passed['id'])
        jobs=self.api('repos/'+repo+'/actions/runs/'+identifier+'/jobs?per_page=100')['jobs']
        steps={s['name']:s.get('conclusion') for j in jobs for s in j.get('steps',[])}
        required=('Update, signature, deployment and rollback regression','Build an independent Windows folder',
                  'Start the packaged EXE and check complete release structure','Bind passed CI checks to the complete build',
                  'Inspect unsigned application and preserved third-party signatures',
                  'Exercise isolated test-certificate signing and rejection paths',
                  'Generate and independently validate actual-package SBOM',
                  'Retain passed build and public summaries',
                  'Attest actual EXE, byte-bound receipt and check reports',
                  'Retain official Sigstore verification bundle')
        if not all(steps.get(name)=='success' for name in required):
            raise PipelineError('CI 缺少通过的测试、构建、实际 EXE 检查或产物保存步骤。')
        artifacts=self.api('repos/'+repo+'/actions/runs/'+identifier+'/artifacts')['artifacts']
        name='MediaFlow-'+version+'-windows-x64-ci-'+identifier
        selected=[a for a in artifacts if a.get('name')==name and a.get('expired') is False]
        if len(selected)!=1 or not re.fullmatch(r'sha256:[a-f0-9]{64}',selected[0].get('digest','')):
            raise PipelineError('CI 验证产物不存在、已过期或缺少服务器 SHA-256。')
        return dict(passed=True,run_id=identifier,commit=commit,url=passed['html_url'],
            artifact={k:selected[0][k] for k in ('id','name','size_in_bytes','digest')},
            scope='successful source-commit CI; downloaded bytes require separate artifact and receipt validation')

    def fetch_build(self,ci,repo,version,root,output,source_sha):
        """Consume the exact passed artifact; never extract into a current release."""
        output=Path(output)
        archive_path=output.with_suffix('.artifact.zip')
        if output.exists() or archive_path.exists():raise PipelineError('CI 产物目标已存在；不会覆盖或合并。')
        artifact=ci['artifact']
        if type(artifact.get('id')) is not int or not 0<artifact.get('size_in_bytes',0)<4*1024**3:
            raise PipelineError('CI 产物 ID 或大小无效。')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}',artifact.get('digest','')):
            raise PipelineError('CI 产物缺少独立服务器摘要。')
        tree=None
        with archive_path.open('xb') as handle:
            try:
                process=subprocess.Popen([self.gh,'api','--method','GET',
                    'repos/'+repo+'/actions/artifacts/'+str(artifact['id'])+'/zip'],
                    cwd=root,stdin=subprocess.DEVNULL,stdout=handle,stderr=subprocess.PIPE,
                    env=self.env,creationflags=NO_WINDOW)
                tree=ProcessTree(process)
                try:process.communicate(timeout=900)
                except subprocess.TimeoutExpired:
                    tree.terminate();process.communicate(timeout=15)
                    raise PipelineError('CI 产物下载超时；保留未完成文件，不使用它。') from None
                if process.returncode:raise PipelineError('CI 产物未能通过真实 GitHub API 下载。')
                handle.flush();os.fsync(handle.fileno())
            finally:
                if tree:tree.close()
        result=extract_ci_artifact(archive_path,output,artifact,repo,ci['commit'],ci['run_id'],version,source_sha)
        proof=verify_ci_provenance(self,repo,ci['commit'],ci['run_id'],output/'verification/ci/build-attestation.json')
        result.update(ci_provenance_verified=True,cryptographic_provenance=proof,
            provenance='Official GitHub OIDC/Sigstore attestation verified for the exact byte-bound receipt, source commit and CI run')
        return result

    def fetch_sbom(self,ci,repo,version,output):
        """Consume the independently validated, officially attested native BOM."""
        output=Path(output)
        if output.exists():raise PipelineError('SBOM 证据目录已存在；不会覆盖。')
        name='MediaFlow-'+version+'-sbom-'+ci['run_id']
        values=self.api('repos/'+repo+'/actions/runs/'+ci['run_id']+'/artifacts?per_page=100')['artifacts']
        selected=[value for value in values if value.get('name')==name and value.get('expired') is False]
        if len(selected)!=1 or not 0<selected[0].get('size_in_bytes',0)<32*1024*1024:
            raise PipelineError('当前构建缺少有界的 SBOM 原始产物。')
        artifact=selected[0]
        code,data,_=self.runner.call([self.gh,'api','--method','GET',
            'repos/'+repo+'/actions/artifacts/'+str(artifact['id'])+'/zip'],
            'ci-sbom-download',timeout=180,logs=False,env=self.env)
        if (code or len(data)!=artifact['size_in_bytes']
                or 'sha256:'+hashlib.sha256(data).hexdigest()!=artifact.get('digest')):
            raise PipelineError('下载的 SBOM 与 GitHub 服务器摘要不一致。')
        allowed={'mediaflow.cdx.json','inventory.json','generation.json','validation.json','SHA256SUMS.txt'}
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            items=archive.infolist()
            if ({item.filename for item in items}!=allowed or len(items)!=len(allowed)
                    or sum(item.file_size for item in items)>64*1024*1024
                    or any(item.flag_bits&1 or stat.S_IFMT(item.external_attr>>16) not in (0,stat.S_IFREG)
                           or item.external_attr&0x400 for item in items)):
                raise PipelineError('SBOM 产物含异常成员或结构。')
            output.mkdir()
            for item in items:
                with (output/item.filename).open('xb') as stream:stream.write(archive.read(item))
        validation=document(output/'validation.json')
        if validation.get('passed') is not True or validation.get('native_coverage_verified') is not True:
            raise PipelineError('CI 的 SBOM 尚未通过原生组件闭合校验。')
        proofs=[verify_ci_provenance(self,repo,ci['commit'],ci['run_id'],output/name)
                for name in ('mediaflow.cdx.json','inventory.json','generation.json','validation.json')]
        generation=document(output/'generation.json')
        if generation.get('github',{}).get('commit')!=ci['commit']:
            raise PipelineError('SBOM 对应的源码提交不一致。')
        return dict(passed=True,artifact={key:artifact[key] for key in ('id','name','size_in_bytes','digest')},
                    original_ci_documents_preserved=True,native_coverage_verified=True,official_proofs=proofs)


def extract_ci_artifact(archive_path,output,artifact,repo,commit,run_id,version,source_sha):
    """Reuse update path validation and the CI receipt verifier before executing EXE."""
    from scripts.ci_build_receipt import verify_receipt
    output=_plain(output).resolve()
    if output.exists():raise PipelineError('CI 解包目录已存在，不能覆盖。')
    allowed={'verification/ci/release-structure.json','verification/ci/update/results.json',
             'verification/ci/build-attestation.json','verification/ci/authenticode-inventory.json',
             'verification/ci/authenticode-tests.json'}
    with frozen_file(archive_path) as stream:
        h=hashlib.sha256();size=0
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block);size+=len(block)
        if size!=artifact['size_in_bytes'] or 'sha256:'+h.hexdigest()!=artifact['digest']:
            raise PipelineError('CI 下载产物的完整大小或 SHA-256 与 GitHub API 不符。')
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            members=archive.infolist()
            if not 1<=len(members)<=5000:raise PipelineError('CI ZIP 文件数量异常。')
            paths=set();expanded=0
            for info in members:
                name=safe_release_path(info.filename.rstrip('/'))
                folded=name.casefold()
                if folded in paths:raise PipelineError('CI ZIP 含重复或大小写冲突路径。')
                paths.add(folded);expanded+=info.file_size
                mode=stat.S_IFMT(info.external_attr>>16)
                if (info.flag_bits&1 or mode not in (0,stat.S_IFREG,stat.S_IFDIR)
                        or info.external_attr&0x400 or info.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED)
                        or expanded>12*1024**3):raise PipelineError('CI ZIP 含加密、链接、异常成员或展开容量。')
                if not info.is_dir() and not (name.startswith('dist-mediaflow-ci/') or name in allowed):
                    raise PipelineError('CI ZIP 含未声明的发布文件或报告路径。')
            output.mkdir()
            for info in members:
                target=output/info.filename
                if info.is_dir():target.mkdir(parents=True,exist_ok=True);continue
                target.parent.mkdir(parents=True,exist_ok=True)
                _plain(target)
                with archive.open(info) as source,target.open('xb') as destination:
                    shutil.copyfileobj(source,destination,1024*1024)
    release=output/'dist-mediaflow-ci'
    result=verify_receipt(output/'verification/ci/build-attestation.json',release,
        expected_version=version,expected_commit=commit,expected_repository=repo,
        expected_run_id=run_id,expected_source_sha256=source_sha)
    return dict(release_directory=str(release),artifact_sha256=artifact['digest'].removeprefix('sha256:'),
        ci_provenance_verified=False,provenance='file binding only; caller must verify the real workflow/API origin',
        receipt_validation=result)


def repository(remote):
    if remote.startswith('git@github.com:'):
        name=remote[len('git@github.com:'):]
    else:
        url=urlsplit(remote)
        if url.scheme!='https' or url.netloc!='github.com' or url.query or url.fragment:
            raise PipelineError('源码 origin 必须是未内嵌凭据的真实 GitHub 仓库。')
        name=url.path.lstrip('/')
    name=name.removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9_.-]*',name):
        raise PipelineError('GitHub 仓库标识无效。')
    return name


def channel_repository(channel):
    url=urlsplit(channel['source_url'])
    match=re.fullmatch(r'/([^/]+/[^/]+)/releases/latest/download/manifest\.json',url.path)
    if url.scheme!='https' or url.netloc!='github.com' or url.query or url.fragment or not match:
        raise PipelineError('此入口只发布到已内置确认的 GitHub 稳定清单；不会猜测或修改更新源。')
    return repository('https://github.com/'+match[1])


def find_vault(value,key_id,channel):
    if value:return _plain(value).resolve()
    roots={Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'MediaFlowPublisher',
        Path.home()/'AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/MediaFlowPublisher'}
    candidates=[]
    for root in roots:
        if not root.is_dir():continue
        for candidate in root.glob('production-ed25519-*'):
            try:
                public=document(candidate/'keys/trusted-public-keys.json')['trusted_keys']
                if public.get(key_id)==channel['trusted_keys'][key_id]:candidates.append(_plain(candidate).resolve())
            except (OSError,ValueError,RuntimeError):continue
    if len(set(candidates))!=1:
        raise PipelineError('未发现唯一匹配的正式 DPAPI 保险库；请指定 -VaultDirectory，不要发送私钥或密码。')
    return candidates[0]


def check_key(private_key_file,public_keys_file,key_id,password_env):
    channel=read_channel(ROOT/'assets/update-channel.json')
    keys=_trusted_keys(public_keys_file,ROOT/'__not_release__',ROOT/'__not_output__')
    if channel['key_state']!='initialized' or keys.get(key_id)!=channel['trusted_keys'].get(key_id) or key_id not in keys:
        raise PipelineError('保险库公钥与内置正式公钥不匹配。')
    key=_private_key(private_key_file=private_key_file,password_env=password_env,blocked=(ROOT,))
    raw=base64.b64encode(key.public_key().public_bytes_raw()).decode('ascii')
    if raw!=keys[key_id]['public_key']:raise PipelineError('保险库私钥与已批准的公钥不匹配。')
    return dict(passed=True,key_id=key_id,key_fingerprint=key_fingerprint(keys[key_id]),secret_logged=False)


class Pipeline:
    def __init__(self,args,*,root=ROOT,runner_factory=Runner,github_factory=GitHub):
        self.args,self.root=args,Path(root).resolve()
        self.git=shutil.which('git')
        self.python=Path(sys.executable).resolve()
        self.gh=args.gh or shutil.which('gh')
        self.pwsh=args.pwsh or shutil.which('pwsh')
        self.dry=args.mode=='dry-run'
        self.read_only_dry=self.dry and not args.certificate_thumbprint
        if args.resume and self.read_only_dry:
            raise PipelineError('未签名只读 DryRun 没有签名恢复点；完整签名恢复需要正式证书配置和原始已证明构建。')
        parse_version(args.version)
        if not 1<=args.ci_wait_seconds<=3600:raise PipelineError('CI 等待时间必须在 1 至 3600 秒之间。')
        base=self.root/'.release-runs'
        base.mkdir(exist_ok=True)
        target=Path(args.run_directory).resolve() if args.run_directory else base/(args.version+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8])
        target=_plain(target).resolve()
        if not target.is_relative_to(base) or target==base:
            raise PipelineError('运行目录必须是本工作区 .release-runs 中的独立子目录，不能写入旧版本或用户目录。')
        self.directory=target
        if args.resume:
            if not target.is_dir():raise PipelineError('恢复目录不存在。')
            self.report=document(target/'report.json')
            if (self.report.get('kind')!='MediaFlow.release-pipeline' or self.report.get('version')!=args.version
                    or self.report.get('mode')!=args.mode or not self.report.get('signed_manifest_sha256')):
                raise PipelineError('只可恢复同模式、同版本且已完成签名的本次流水线；不覆盖原始证据。')
            if self.report.get('passed'):raise PipelineError('本次运行已完成，请阅读原报告；不会重复发布。')
        else:
            target.mkdir()
            self.report=dict(schema_version=1,kind='MediaFlow.release-pipeline',mode=args.mode,version=args.version,
                started_at=utc(),passed=False,published=False,public_online_verified=False,
                production_chain_verified=False,remote_writes=False,published_by_this_run=False,events=[],stage='configuration')
        self.attempt=target/('attempt-'+uuid.uuid4().hex[:10]);self.attempt.mkdir()
        self.runner=runner_factory(self.root,self.attempt)
        self.github_factory=github_factory
        self.lock=None

    def event(self,stage,status='running',**details):
        starts=getattr(self,'_stage_starts',{})
        if status=='running':starts[stage]=time.monotonic()
        elif stage in starts:details.setdefault('elapsed_seconds',round(time.monotonic()-starts.pop(stage),4))
        self._stage_starts=starts
        self.report['stage']=stage
        self.report['events'].append(dict(time=utc(),stage=stage,status=status,**details))
        save(self.directory/'report.json',self.report)
        print('阶段：'+stage+' ['+status+']',flush=True)

    def command(self,program,*args,stage,timeout=600,env=None,logs=True):
        self.event(stage)
        try:
            result=self.runner.call([program,*args],stage,timeout=timeout,env=env,logs=logs)
            if result[0]!=0:raise PipelineError('阶段进程未成功退出：'+stage)
        except BaseException as error:
            self.event(stage,'failed',error_type=type(error).__name__)
            raise
        self.event(stage,'passed',exit_code=result[0])
        return result[1]

    def git_value(self,*args):
        return self.runner.call([self.git,*args],'git-read',timeout=45,logs=False)[1].decode('utf-8').strip()

    def preflight(self):
        version=parse_version(self.args.version)
        if version.is_prerelease or version.local or self.args.version!=APP_VERSION:
            raise PipelineError('版本必须与当前源码 APP_VERSION 完全一致；此工具不会为演练增加产品版本。')
        if not self.git or not self.gh or not self.pwsh:
            raise PipelineError('需要 Git、GitHub CLI、PowerShell 7.2+；可通过 -Gh 指定现有 CLI。')
        if self.git_value('status','--porcelain=v1','--untracked-files=all'):
            raise PipelineError('Git 工作区不干净；请先审核并提交代码，不能跳过源文件审计。')
        commit=self.git_value('rev-parse','HEAD')
        if self.args.resume and self.report.get('commit')!=commit:raise PipelineError('源码提交已变化，不能沿用旧签名或恢复上下文。')
        self.report['commit']=commit
        self.source_repo=repository(self.git_value('remote','get-url','origin'))
        self.channel=read_channel(self.root/'assets/update-channel.json')
        if self.channel['key_state']!='initialized' or not self.channel['trusted_keys']:
            raise PipelineError('正式更新源或可信公钥尚未初始化；不会使用测试密钥替代。')
        self.release_repo=channel_repository(self.channel)
        self.key_id=self.args.key_id
        if not self.key_id:
            if len(self.channel['trusted_keys'])!=1:raise PipelineError('有多个可信公钥，请明确选择 -KeyId。')
            self.key_id=next(iter(self.channel['trusted_keys']))
        if self.key_id not in self.channel['trusted_keys']:raise PipelineError('指定的正式 key_id 不受内置信任。')
        self.report.update(source_repository=self.source_repo,release_repository=self.release_repo,
            key_id=self.key_id,key_fingerprint=key_fingerprint(self.channel['trusted_keys'][self.key_id]))
        self.github=self.github_factory(self.runner,self.gh,self.git)
        user=self.github.api('user')['login']
        for name in (self.source_repo,self.release_repo):
            info=self.github.repo(name)
            if info.get('private') is not False or info.get('owner',{}).get('login','').casefold()!=user.casefold() or info.get('permissions',{}).get('admin') is not True:
                raise PipelineError('仅允许当前发布者明确拥有管理权限的已确认公开仓库。')
            if name==self.source_repo:self.source_info=info
        self.current=self.github.release(self.release_repo,self.args.version)
        self.report['existing_release']=dict(exists=bool(self.current),published=bool(self.current and not self.current['draft']))
        if self.current and not self.current['draft'] and not self.dry and not self.args.resume:
            raise PipelineError('此版本已经正式发布；原 Release 和资产保持不变。只可 DryRun 或恢复原发布后的验收。')
        if not self.dry:
            branch=quote(self.source_info['default_branch'],safe='')
            ref=self.github.api('repos/'+self.source_repo+'/git/ref/heads/'+branch)
            if ref.get('object',{}).get('sha')!=commit:
                raise PipelineError('正式发布只接受远程默认分支当前提交；请先完成代码审查和合并。')
        tag='v'+self.args.version
        code,out,_=self.runner.call([self.git,'rev-parse','--verify','refs/tags/'+tag+'^{}'],'source-tag-read',
            timeout=30,logs=False,allow_failure=True)
        self.tag_conflict=not code and out.decode().strip()!=commit
        if self.tag_conflict and not self.dry:raise PipelineError('源码版本标签已指向其他提交；禁止覆盖标签。')
        self.report['source_tag']=dict(name=tag,exists=not bool(code),conflicts_with_commit=self.tag_conflict,will_create=bool(code) and not self.dry)
        remote_tag=self.github.api('repos/'+self.source_repo+'/git/ref/tags/'+tag,missing=True)
        remote_commit=None
        if remote_tag:
            reference=remote_tag.get('object',{})
            if reference.get('type')=='tag':
                reference=self.github.api('repos/'+self.source_repo+'/git/tags/'+reference['sha']).get('object',{})
            if reference.get('type')!='commit':raise PipelineError('远程源码标签不能解析为明确提交。')
            remote_commit=reference.get('sha')
            if remote_commit!=commit and not self.dry:raise PipelineError('远程源码版本标签已指向其他提交；禁止覆盖。')
        self.report['source_tag'].update(remote_exists=bool(remote_tag),remote_commit=remote_commit)
        latest=self.github.api('repos/'+self.release_repo+'/releases/latest',missing=True)
        if latest and not self.dry and not self.args.resume:
            newest=parse_version(latest.get('tag_name','').removeprefix('v'))
            if version<=newest:raise PipelineError('正式入口不发布相同或更低版本；不会将旧版本重新标记为 latest。')
        self.report['security']=self.github.security(self.source_repo,commit,
            default_branch=self.source_info['default_branch'],wait_seconds=self.args.ci_wait_seconds)
        if self.report['security'].get('passed') is not True:
            raise PipelineError('当前提交的安全工作流未通过；禁止读取正式保险库或继续发布。')
        if not self.read_only_dry:
            self.auth_options=production_options(self.args.certificate_thumbprint,self.args.certificate_store,
                self.args.timestamp_url,self.args.sign_tool_path)
            self.report['authenticode_configuration']=authenticode('Inspect',
                self.attempt/'authenticode-configuration.json',options=self.auth_options,
                pwsh=self.pwsh,runner=self.runner.call,stage='authenticode-certificate-preflight')
            configured=Path(self.args.security_python) if self.args.security_python else self.root/'.security-venv/Scripts/python.exe'
            if not configured.is_file():
                raise PipelineError('缺少固定版本的独立 SBOM 校验环境；请准备 .security-venv 或指定 -SecurityPython。')
            self.security_python=_plain(configured).resolve()
        self.vault=None
        if not self.read_only_dry:
            self.vault=find_vault(self.args.vault_directory,self.key_id,self.channel)
            if self.vault.is_relative_to(self.root):raise PipelineError('正式密钥保险库必须在源码和运行目录之外。')
        self.notes=_plain(self.args.release_notes_file or self.root/'RELEASE_NOTES.md').resolve()
        if not self.notes.is_file():raise PipelineError('发布说明文件不存在。')
        self.old=_plain(self.args.old_release_dir).resolve() if self.args.old_release_dir else self.find_old()
        if self.old is None:raise PipelineError('需要可实际启动的旧发布目录以验收回退；请指定 -OldReleaseDirectory。')
        if (not (self.old/'MediaFlow.exe').is_file()
                or parse_version(_read_stamp(self.old)['app_version'])>=version):
            raise PipelineError('回退目录必须包含可用 EXE，且版本低于本次目标。')
        self.report['old_release_directory']=str(self.old)
        if not self.read_only_dry:
            self.command(self.pwsh,'-NoLogo','-NoProfile','-NonInteractive','-File',
                self.root/'scripts/publisher_windows_vault.ps1','-Operation','Check','-VaultDirectory',self.vault,
                '-KeyId',self.key_id,'-Python',self.python,stage='formal-vault-check',timeout=90,logs=False)
        self.ci=self.github.ci(self.source_repo,commit,self.args.version,run_id=self.args.ci_run_id,
            wait_seconds=self.args.ci_wait_seconds)
        self.report['ci']=self.ci
        self.event('preflight','passed')

    def find_old(self):
        choices=[]
        for root in self.root.parent.glob('m3u8-downloader-*'):
            for candidate in root.glob('dist*'):
                try:
                    candidate=_plain(candidate).resolve();stamp=_read_stamp(candidate)
                    version=parse_version(stamp['app_version'])
                    if version<parse_version(self.args.version) and (candidate/'MediaFlow.exe').is_file():choices.append((version,candidate))
                except (OSError,ValueError,RuntimeError):continue
        return max(choices,key=lambda v:v[0])[1] if choices else None

    def prepare(self):
        audit=self.attempt/'source-audit.json'
        self.command(self.python,'-B',self.root/'scripts/audit_public_source.py','--root',self.root,'--tracked',
            '--reviews',self.root/'docs/public-source-reviews.json','--report',audit,stage='source-audit',timeout=120)
        unit=self.attempt/'unit'
        self.command(self.python,'-B',self.root/'tests/run_update_tests.py','--report-dir',unit,
            stage='incremental-tests',timeout=1800)
        tests=document(unit/'results.json')
        if (tests.get('passed') is not True or tests.get('source_unchanged') is not True
                or tests.get('failures')!=0 or tests.get('errors')!=0 or tests.get('tests',0)<1
                or any(g.get('unexpected_skips') for g in tests.get('groups',[]))):
            raise PipelineError('必要增量测试未完整通过，不能继续构建或签名。')
        self.report['tests']={k:tests[k] for k in ('tests','failures','errors','skipped','passed','source_sha256')}
        self.event('verified-windows-ci-artifact')
        if self.args.existing_release_dir:
            self.release=_plain(self.args.existing_release_dir).resolve()
            if self.directory.is_relative_to(self.release):raise PipelineError('运行目录不能位于既有发布目录中。')
            self.report['build']=dict(mode='reused-accepted-release',new_exe_built=False)
            from scripts.ci_build_receipt import verify_receipt
            receipt=self.release.parent/'verification/ci/build-attestation.json'
            if not receipt.is_file():
                raise PipelineError('复用需要原 CI 目录及相邻的已证明构建收据；不能用本地验收替代来源验证。')
            bound=verify_receipt(receipt,self.release,expected_version=self.args.version,
                expected_repository=self.source_repo,expected_commit=self.report['commit'],
                expected_run_id=self.ci['run_id'],expected_source_sha256=source_digest(self.root))
            proof=verify_ci_provenance(self.github,self.source_repo,self.report['commit'],self.ci['run_id'],receipt,
                default_branch=self.source_info['default_branch'])
            self.report['build'].update(byte_binding=bound,cryptographic_provenance=proof)
        else:
            fetched=self.github.fetch_build(self.ci,self.source_repo,self.args.version,self.root,
                self.directory/'ci-build',source_digest(self.root))
            self.release=Path(fetched['release_directory'])
            self.report['ci']['artifact_verification']=fetched
            self.report['build']=dict(mode='passed-windows-ci-artifact',new_local_exe_built=False)
        self.event('verified-windows-ci-artifact','passed')
        self.unsigned_release=self.release
        self.report['unsigned_release_directory']=str(self.unsigned_release)
        self.ci_sbom=self.directory/'ci-sbom'
        self.event('verified-native-sbom')
        self.report['sbom']=self.github.fetch_sbom(self.ci,self.source_repo,self.args.version,self.ci_sbom)
        source_receipt=self.unsigned_release.parent/'verification/ci/build-attestation.json'
        original_receipt=document(source_receipt)
        original_generation=document(self.ci_sbom/'generation.json')
        # The authenticated per-PE inventory uses the independent SBOM reader's
        # 32 MiB policy. Ordinary update/receipt JSON retains its 2 MiB default.
        if (original_generation.get('receipt_sha256')!=_file_hash(source_receipt)
                or document(self.ci_sbom/'inventory.json',maximum=32*1024*1024).get('release_files')!=original_receipt.get('files')):
            raise PipelineError('原始 CI 收据、实际发布文件和已证明 SBOM 不属于同一构建，禁止签名。')
        self.event('verified-native-sbom','passed')
        if not self.read_only_dry:
            signed_release=self.directory/'authenticode-build'
            auth=authenticode('Sign',self.attempt/'authenticode-signing.json',release=self.unsigned_release,
                output=signed_release,options=self.auth_options,pwsh=self.pwsh,runner=self.runner.call,
                stage='authenticode-sign-and-verify')
            self.report['signing_derivation']=signing_derivation(self.unsigned_release,signed_release,
                source_receipt,auth,self.attempt/'authenticode-derivation.json')
            self.report['authenticode']=auth
            self.release=signed_release
        identity=self.attempt/'runtime-identity.json'
        self.command(self.release/'MediaFlow.exe','--build-info',identity,stage='actual-exe-identity',timeout=120)
        runtime=document(identity);stamp=_read_stamp(self.release)
        if (runtime.get('packaged') is not True or runtime.get('app_version')!=self.args.version
                or runtime.get('source_sha256')!=source_digest(self.root)
                or runtime.get('build_id')!=stamp['build_id']):raise PipelineError('实际 EXE 不对应当前产品版本/运行源码，拒绝复用或签名。')
        structure=self.attempt/'release-structure.json'
        self.command(self.python,'-B',self.root/'scripts/check_release_structure.py','--release-dir',self.release,
            '--expected-version',self.args.version,'--runtime-identity',identity,'--report',structure,
            stage='complete-release-structure',timeout=180)
        self.report['build'].update(build_id=stamp['build_id'],source_sha256=stamp['source_sha256'],
            exe_sha256=_file_hash(self.release/'MediaFlow.exe'),release_directory=str(self.release))
        if self.read_only_dry:
            self.event('unsigned-authenticode-inspection')
            self.report['authenticode']=authenticode('Inspect',self.attempt/'unsigned-authenticode.json',
                release=self.release,mode='CI',pwsh=self.pwsh,runner=self.runner.call,stage='unsigned-authenticode-inspection')
            self.event('unsigned-authenticode-inspection','passed',exit_code=0)
            return
        reports=[_plain(p).resolve() for p in self.args.acceptance_report]
        if reports and not self.args.existing_release_dir:raise PipelineError('新构建不能沿用旧验收报告。')
        if not reports:
            native=self.attempt/'native-acceptance'
            self.command(self.release/'MediaFlow.exe','--tracks-check',native,'--tracks-organization','--native-smoke',
                stage='actual-exe-local-protocols',timeout=750,env=dict(safe_environment(),QT_QPA_PLATFORM='windows'))
            self.command(self.release/'MediaFlow.exe','--tracks-check',native,'--tracks-restart','--native-smoke',
                stage='actual-exe-local-restart',timeout=240,env=dict(safe_environment(),QT_QPA_PLATFORM='windows'))
            reports=[native/'result.json',native/'restart-result.json']
        receipt=self.attempt/'acceptance-receipt.json'
        argv=['-B',self.root/'scripts/publish_release.py','receipt','--release-dir',self.release,'--output',receipt]
        for path in reports:argv+=['--report',path]
        self.command(self.python,*argv,stage='acceptance-receipt',timeout=180)
        self.report.update(release_directory=str(self.release),acceptance_receipt=str(receipt))
        build_id=stamp['build_id'];name='MediaFlow-'+self.args.version+'-'+build_id+'.zip'
        self.package_url='https://github.com/'+self.release_repo+'/releases/download/v'+self.args.version+'/'+name
        self.signed=self.directory/'signed'
        self.command(self.pwsh,'-NoLogo','-NoProfile','-NonInteractive','-File',
            self.root/'scripts/publisher_windows_vault.ps1','-Operation','Publish','-VaultDirectory',self.vault,
            '-KeyId',self.key_id,'-Python',self.python,'-ReleaseDirectory',self.release,'-OutputDirectory',self.signed,
            '-AcceptanceReceipt',receipt,'-PackageUrl',self.package_url,'-PublishedAt',datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            '-ReleasePage','https://github.com/'+self.release_repo+'/releases/tag/v'+self.args.version,
            '-ReleaseNotesFile',self.notes,'-MinimumAppVersion',self.args.minimum_app_version,
            '-CertificateThumbprint',self.args.certificate_thumbprint,
            '-CertificateStore',self.args.certificate_store,'-TimestampUrl',self.args.timestamp_url,
            '-SignToolPath',self.args.sign_tool_path,
            stage='formal-signing',timeout=1800,logs=False)
        self.validate_signed()
        if not self.read_only_dry:
            publication=self.directory/'publication-sbom'
            self.command(self.security_python,'-B',self.root/'scripts/release_security.py','publication-sbom',
                '--original-directory',self.ci_sbom,'--signed-release',self.release,
                '--original-receipt',source_receipt,'--derivation',self.attempt/'authenticode-derivation.json',
                '--manifest',self.signed/'release.json','--output',publication,
                stage='signed-final-zip-sbom-validation',timeout=360)
            final=document(publication/'publication-generation.json')
            if final.get('passed') is not True or final.get('package')!=self.manifest['package']:
                raise PipelineError('最终签后 SBOM 未能绑定真实 ZIP，禁止发布。')
            self.report['publication_sbom']=final
            self.report['publication_sbom_directory']=str(publication)
            self.args.additional_asset += [str(publication/name) for name in
                ('final-mediaflow.cdx.json','final-inventory.json','final-sbom-validation.json','publication-generation.json')]
            # Rebuild the exact upload inventory with the verified sidecars.
            self.validate_signed()
        self.report.update(signed_directory=str(self.signed),signed_manifest_sha256=_file_hash(self.signed/'release.json'))
        self.event('signed-package','passed',signature_verified=True,package=self.manifest['package'])

    def validate_signed(self):
        manifest,tag,assets,records=prepare_assets(self.signed,self.release_repo,self.args.additional_asset)
        if manifest['version']!=self.args.version or manifest['build_id']!=_read_stamp(self.release)['build_id']:
            raise PipelineError('已签名资产的版本或构建标识不匹配。')
        _match_release_inventory(self.release,{entry['path']:entry for entry in manifest['files']},None,None,'accepted-release-recheck')
        self.manifest,self.records=manifest,records
        self.report['package']=manifest['package']

    def publish(self):
        if self.dry:
            self.report['write_plan']=dict(executed=False,source_tag='create only during Publish',
                operations=['create_or_resume_draft','upload_exact_assets','verify_authenticated_draft_download','publish_verified_draft'],
                reason='DryRun never performs a GitHub or source-tag mutation')
            self.event('github-publish','not_executed_dry_run')
            return
        current=self.github.release(self.release_repo,self.args.version)
        if current and not current['draft']:
            if not self.args.resume:raise PipelineError('版本已经发布；不会再次签名或覆盖。')
            urls=verified_asset_urls(current,self.records,self.release_repo,'v'+self.args.version,published=True)
            self.report.update(published=True,release_url=current['html_url'],remote_writes=False)
            self.event('github-publish','resumed_read_only',assets=len(urls))
            return
        if not self.report['source_tag']['remote_exists']:
            if not self.report['source_tag']['exists']:
                self.command(self.git,'-c','tag.gpgSign=false','tag','-a','v'+self.args.version,'-m',
                    'MediaFlow '+self.args.version+' accepted release '+self.report['commit'],stage='source-tag-create',timeout=30)
            self.command(self.git,'push','origin','refs/tags/v'+self.args.version,stage='source-tag-push',timeout=120)
            self.report['source_tag'].update(exists=True,remote_exists=True,remote_commit=self.report['commit'])
        report=self.attempt/'github-publication.json'
        args=['-B',self.root/'scripts/publish_github_release.py','--gh',self.gh,'--repository',self.release_repo,
            '--artifacts',self.signed,'--notes',self.notes,'--report',report,'--publish',
            '--certificate-thumbprint',self.args.certificate_thumbprint,'--certificate-store',self.args.certificate_store,
            '--timestamp-url',self.args.timestamp_url,'--sign-tool-path',self.args.sign_tool_path,'--pwsh',self.pwsh]
        for asset in self.args.additional_asset:args+=['--asset',asset]
        self.report['remote_writes']=True
        self.command(self.python,*args,stage='github-draft-upload-verify-publish',timeout=7200,
            env=self.github.env,logs=False)
        result=document(report)
        if not result.get('passed') or not result.get('draft_verified') or not result.get('published_assets_verified') or not result.get('published'):
            raise PipelineError('现有发布器未确认草稿和最终公开资产；保留阶段记录。')
        self.report.update(published=True,published_by_this_run=True,release_url=result['release_url'])
        self.event('github-publish','passed',draft_verified=True)

    def public_acceptance(self):
        current=self.github.release(self.release_repo,self.args.version)
        if not current or current['draft']:
            if not self.dry:raise PipelineError('正式 Release 尚不可公开读取。')
            self.event('public-existing-release','not_available',reason='No public release for this rehearsal version; no public claim')
            return
        public_stage='public-existing-release' if self.dry else 'public-new-release'
        self.event(public_stage)
        _match_release_inventory(self.release,{entry['path']:entry for entry in self.manifest['files']},None,None,'public-client-release-recheck')
        directory=self.attempt/'public-acceptance'
        self.command(self.python,'-B',self.root/'scripts/accept_release_update.py','--release-dir',self.release,
            '--old-release-dir',self.old,'--expected-version',self.args.version,'--report-dir',directory,
            '--timeout','2400',stage='packaged-public-update-acceptance',timeout=2450)
        result=document(directory/'result.json')
        if result.get('passed') is not True or result.get('public_online_verified') is not True:
            raise PipelineError('实际 EXE 公网更新、部署、启动与回退未全部通过。')
        remote=result['public_manifest']['package']
        if not self.dry and remote!=self.manifest['package']:
            raise PipelineError('客户端实际获得的公网 ZIP 与本次正式签名资产不同。')
        if result['runtime_identity'].get('build_id')!=_read_stamp(self.release)['build_id']:
            raise PipelineError('公网验收不是所选真实 EXE 构建。')
        self.report['public_acceptance']=dict(passed=True,public_online_verified=True,
            scope='existing_published_release' if self.dry else 'this_newly_published_signed_release',
            report=str(directory/'result.json'),checks=len(result['checks']),public_package=remote,
            staged_package_was_publicly_verified=not self.dry)
        self.report['public_online_verified']=True
        self.report['production_chain_verified']=not self.dry and self.report['published'] is True
        self.event(public_stage,'passed')
        self.event('public-acceptance','passed')

    def run(self):
        try:
            self.lock=(self.directory/'.pipeline.lock').open('x',encoding='utf-8')
            self.lock.write(str(os.getpid()));self.lock.flush()
        except FileExistsError:raise PipelineError('运行目录已有活动或中断锁；保留原目录，不能并发重入。') from None
        try:
            self.event('preflight')
            self.preflight()
            if self.args.resume:
                self.release=_plain(self.report['release_directory']).resolve()
                self.signed=_plain(self.report['signed_directory']).resolve()
                if _file_hash(self.signed/'release.json')!=self.report['signed_manifest_sha256']:
                    raise PipelineError('恢复目录中的已签名清单发生变化。')
                if not self.read_only_dry:
                    from scripts.ci_build_receipt import verify_receipt
                    unsigned=_plain(self.report.get('unsigned_release_directory','')).resolve()
                    original=unsigned.parent/'verification/ci/build-attestation.json'
                    verify_receipt(original,unsigned,expected_version=self.args.version,
                        expected_repository=self.source_repo,expected_commit=self.report['commit'],
                        expected_run_id=self.ci['run_id'],expected_source_sha256=source_digest(self.root))
                    verify_ci_provenance(self.github,self.source_repo,self.report['commit'],self.ci['run_id'],original,
                        default_branch=self.source_info['default_branch'])
                    original_bom=self.directory/'ci-sbom'
                    for name in ('mediaflow.cdx.json','inventory.json','generation.json','validation.json'):
                        verify_ci_provenance(self.github,self.source_repo,self.report['commit'],self.ci['run_id'],original_bom/name,
                            default_branch=self.source_info['default_branch'])
                    fresh=authenticode('Verify',self.attempt/'resume-authenticode.json',release=self.release,
                        options=self.auth_options,pwsh=self.pwsh,runner=self.runner.call,stage='resume-native-authenticode')
                    signing_derivation(unsigned,self.release,original,fresh,self.attempt/'resume-signing-derivation.json')
                    folder=_plain(self.report.get('publication_sbom_directory','')).resolve()
                    final=document(folder/'publication-generation.json')
                    if final!=self.report.get('publication_sbom') or final.get('passed') is not True:
                        raise PipelineError('恢复需要原始签后 SBOM 验证证据；不会回退到未经验证的资产。')
                    for name,field in (('final-mediaflow.cdx.json','final_sbom_sha256'),('final-inventory.json','final_inventory_sha256')):
                        if _file_hash(folder/name)!=final[field]:raise PipelineError('签后 SBOM 证据已变化，停止恢复。')
                    self.args.additional_asset += [str(folder/name) for name in
                        ('final-mediaflow.cdx.json','final-inventory.json','final-sbom-validation.json','publication-generation.json')]
                self.validate_signed()
            else:self.prepare()
            if self.read_only_dry:
                self.report.update(passed=True,finished_at=utc(),production_ready=False,
                    dry_run_level='unsigned-build-and-native-sbom',formal_signed=False,
                    formal_certificate='not_configured',signed_package_created=False,
                    write_plan=dict(executed=False,operations=[],scope='Read-only rehearsal; no signing vault or publisher writes.'))
                self.event('unsigned-dry-run-complete','passed')
                return self.report
            self.publish()
            self.public_acceptance()
            self.report.update(passed=True,finished_at=utc())
            self.event('dry-run-complete' if self.dry else 'production-release-complete','passed')
            return self.report
        except BaseException as error:
            for stage in list(getattr(self,'_stage_starts',{})):
                self.event(stage,'failed',error_type=type(error).__name__)
            # An interrupted publisher may have completed GitHub publication.
            # Do not claim rollback or delete any remote assets on failure.
            if hasattr(self,'github') and hasattr(self,'release_repo'):
                try:
                    actual=self.github.release(self.release_repo,self.args.version)
                    if actual and actual.get('draft') is False and not self.dry:
                        self.report.update(published=True,release_url=actual['html_url'])
                except Exception:pass
            self.report.update(passed=False,error_type=type(error).__name__,finished_at=utc(),
                error=str(error) if isinstance(error,(PipelineError,ReleaseSecurityError)) else '阶段未完成；保留本次证据、旧版本和远程资产。')
            save(self.directory/'report.json',self.report)
            if isinstance(error,KeyboardInterrupt):raise
            raise PipelineError(self.report['error']) from None
        finally:
            if self.lock:
                self.lock.close();(self.directory/'.pipeline.lock').unlink()


def parser():
    result=argparse.ArgumentParser(description=__doc__)
    commands=result.add_subparsers(dest='command',required=True)
    key=commands.add_parser('check-key')
    for name in ('private-key-file','public-keys-file','key-id','password-env'):key.add_argument('--'+name,required=True)
    run=commands.add_parser('run')
    run.add_argument('--version',required=True);run.add_argument('--mode',choices=('dry-run','publish'),required=True)
    for name in ('gh','pwsh','vault-directory','key-id','run-directory','existing-release-dir','old-release-dir','ci-run-id','release-notes-file',
                 'certificate-thumbprint','timestamp-url','sign-tool-path','security-python'):
        run.add_argument('--'+name,default='')
    run.add_argument('--certificate-store',choices=('CurrentUser','LocalMachine'),default='CurrentUser')
    run.add_argument('--ci-wait-seconds',type=int,default=1200)
    run.add_argument('--minimum-app-version',default='2.0.1')
    run.add_argument('--acceptance-report',action='append',default=[])
    run.add_argument('--additional-asset',action='append',default=[])
    run.add_argument('--resume',action='store_true')
    return result


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command=='check-key':
            value=check_key(args.private_key_file,args.public_keys_file,args.key_id,args.password_env)
        else:
            pipeline=Pipeline(args);value=pipeline.run()
        print(json.dumps({k:value[k] for k in ('passed','mode','version','published','public_online_verified','production_chain_verified','key_id') if k in value},ensure_ascii=False))
        if args.command=='run':print('发布报告：'+str(pipeline.directory/'report.json'))
        return 0
    except Exception as error:
        print(json.dumps({'passed':False,'error_type':type(error).__name__,
            'error':str(error) if isinstance(error,PipelineError) else '无法安全完成流水线；请检查本次阶段报告。'},ensure_ascii=False),file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
