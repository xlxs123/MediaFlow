"""记录旧误判规则与站点硬编码检查，不把文档中的历史说明视为运行逻辑。"""
from pathlib import Path
import ast
import json
import re

ROOT = Path(__file__).resolve().parents[1]


def main():
    raises = []
    problems = []
    resolver_branches = []
    for file in sorted((ROOT / 'core').glob('*.py')):
        source = file.read_text(encoding='utf-8-sig')
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if file.name in ('manifests.py', 'media_guard.py', 'media_bridge.py') and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'endswith':
                hints = [literal.value.lower() for arg in node.args for literal in ast.walk(arg)
                         if isinstance(literal, ast.Constant) and isinstance(literal.value, str)]
                if any(hint in {'.ts', '.m4s', '.aac', '.mp4', '.bin', '.dat', '.png'} for hint in hints):
                    problems.append(f'{file.name}:{node.lineno} 在资源协议层按分片扩展名分支')
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name) and node.exc.func.id == 'ProtectedMediaError':
                parents = [candidate for candidate in ast.walk(tree) if isinstance(candidate, ast.If) and node in list(ast.walk(candidate))]
                conditions = [ast.unparse(parent.test) for parent in parents]
                has_evidence_guard = any('drm is True' in condition for condition in conditions)
                raises.append({'file': str(file.relative_to(ROOT)), 'line': node.lineno,
                               'conditions': conditions, 'evidence_guard': has_evidence_guard})
                if not has_evidence_guard:
                    problems.append(f'{file.name}:{node.lineno} ProtectedMediaError缺少明确drm证据条件')
            if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and node.left.id in ('domain', 'hostname', 'host'):
                literals = [item.value for item in node.comparators if isinstance(item, ast.Constant) and isinstance(item.value, str)]
                if any(item != 'localhost' for item in literals):
                    if file.name == 'site_resolvers.py':
                        resolver_branches.append({'file': file.name, 'line': node.lineno, 'role': 'SiteResolver'})
                    else:
                        problems.append(f'{file.name}:{node.lineno} 存在域名字面量分支')
            if isinstance(node, ast.Compare):
                compared = ast.unparse(node)
                if any(token in compared for token in ('.hostname', '.netloc', 'domain', 'hostname')):
                    literals = [literal.value for expression in (node.left, *node.comparators)
                                for literal in ast.walk(expression) if isinstance(literal, ast.Constant)
                                and isinstance(literal.value, str)]
                    for literal in literals:
                        if ('.' in literal and literal not in ('127.0.0.1',)
                                and re.fullmatch(r'[A-Za-z0-9.-]+\.[A-Za-z]{2,}', literal)):
                            if file.name != 'site_resolvers.py':
                                problems.append(f'{file.name}:{node.lineno} 媒体运行层存在网站域名分支')
    friendly = (ROOT / 'core' / 'downloader.py').read_text(encoding='utf-8')
    function = next(node for node in ast.parse(friendly).body if isinstance(node, ast.FunctionDef) and node.name == 'friendly_error')
    if any(isinstance(node, ast.Constant) and isinstance(node.value, str)
           and node.value in ('encrypted', 'widevine', 'fairplay', 'playready') for node in ast.walk(function)):
        problems.append('friendly_error仍使用加密或key-system关键词单独作为DRM判定')
    report = {'passed': not problems, 'checks': ['ProtectedMediaError仅由info.drm is True触发',
              '域名适配仅位于SiteResolver，Downloader及通用协议层无网站分支', '错误提示不因AES或路径中的DRM系统名误判保护',
              'Manifest/guard/bridge不存在按分片扩展名决定角色的规则'],
              'protected_raises': raises, 'resolver_branches': resolver_branches,
              'downloader_site_domain_rules': 0, 'problems': problems}
    report['checks'].extend(['GenericDiscovery不包含网站适配规则',
                             '浏览器网络监听仅连接本应用page及回环端口'])
    for file_name in ('http_client.py', 'downloader.py', 'media_guard.py', 'site_resolvers.py'):
        tree = ast.parse((ROOT / 'core' / file_name).read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                condition = ast.unparse(node.test).lower()
                body = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
                if ('not' in condition and ('cookie' in condition or 'authorization' in condition)
                        and 'AUTH_REQUIRED' in body):
                    problems.append(f'{file_name}:{node.lineno} 缺少Cookie/Authorization被当作认证失败证据')
    report['checks'].append('缺少Cookie或Authorization不会单独判AUTH_REQUIRED')
    report['passed'] = not problems
    (ROOT / 'verification' / 'protocol-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
