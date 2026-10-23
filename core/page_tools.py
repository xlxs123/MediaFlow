"""当前页面的临时操作辅助；不自动点击，不访问跨源 iframe 内容。"""


_PAGE_TOOLS = r"""
(() => {
    const slot = '__streamPageTools_v1';
    if (window[slot]) return window[slot];
    const styles = new Map(), scrolls = new Map();
    let selected = null, windowScroll = null;
    const auth = /(?:\b(?:log[ -]?in|sign[ -]?in|password|pay[ -]?wall|payment|subscribe|subscription|membership|members?|vip|premium|permission|captcha|consent|authorization|purchase|checkout|access[ -]?restricted)\b|登录|登陆|会员|付费|支付|购买|订阅|权限|授权|验证码|验证身份)/i;
    const player = /(?:^|[\s_-])(?:player|video|videojs|vjs|jwplayer|plyr|dplayer|xgplayer|audio|canvas)(?:$|[\s_-])/i;
    const closeText = /^(?:\s*(?:close|dismiss|skip(?:\s+ad)?|关闭|关闭广告|跳过(?:广告)?|取消|×|✕|✖|x)\s*)$/i;
    const closeLabel = /(?:\b(?:close|dismiss|skip)\b|关闭|跳过)/i;
    const media = 'video,audio,canvas,[data-player],[data-video-player]';
    const playerNode = node => player.test([node.id, node.className, node.tagName]
        .filter(v => typeof v === 'string').join(' ').replace(/([a-z])([A-Z])/g, '$1 $2'));
    const visible = node => {
        const style = getComputedStyle(node), r = node.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0 && r.width > 0 && r.height > 0;
    };
    const rect = node => {
        const r = node.getBoundingClientRect();
        const width = Math.max(0, Math.min(innerWidth, r.right) - Math.max(0, r.left));
        const height = Math.max(0, Math.min(innerHeight, r.bottom) - Math.max(0, r.top));
        return {r, coverage: width * height / Math.max(1, innerWidth * innerHeight),
                inside: r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight};
    };
    const reason = node => {
        if (!node || node === document.body || node === document.documentElement) return 'page_root';
        if (node.matches(media) || node.querySelector(media)) return 'player';
        if (Array.from(node.querySelectorAll('[id],[class]')).slice(0, 512).some(playerNode)) return 'player';
        for (let parent = node; parent && parent !== document.body; parent = parent.parentElement) {
            if (playerNode(parent)) return 'player';
        }
        // iframe 可能承载主播放器；不能把不透明的子文档当成可隐藏广告。
        if (node.tagName === 'IFRAME' || node.querySelector('iframe')) return 'iframe';
        const attributes = [node.id, node.className, node.getAttribute('aria-label'), node.getAttribute('title')]
            .filter(v => typeof v === 'string').join(' ').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[_-]/g, ' ');
        if (auth.test(attributes) || auth.test((node.innerText || '').slice(0, 8192)) ||
            node.querySelector('input[type=password],[autocomplete*=password],[data-paywall],[data-login],[data-auth],[data-permission]')) return 'protected_layer';
        return '';
    };
    const closeButtons = node => Array.from(node.querySelectorAll('button,a,[role=button]')).slice(0, 128)
        .filter(button => visible(button) && !button.disabled && button.getAttribute('aria-disabled') !== 'true' &&
            (closeText.test((button.innerText || button.textContent || '').slice(0, 128)) ||
             closeLabel.test([button.getAttribute('aria-label'), button.title].filter(Boolean).join(' '))))
        .sort((a, b) => Number(rect(b).inside) - Number(rect(a).inside));
    const collect = () => {
        const found = [];
        for (const node of Array.from(document.querySelectorAll('body *')).slice(0, 4096)) {
            if (!visible(node)) continue;
            const style = getComputedStyle(node);
            if (!['fixed', 'absolute', 'sticky'].includes(style.position)) continue;
            const geometry = rect(node);
            if (geometry.coverage <= 0.4) continue;
            const z = Number.parseInt(style.zIndex, 10);
            if (style.position === 'absolute' && (!Number.isFinite(z) || z < 1)) continue;
            const blocked = reason(node);
            if (blocked === 'player' || blocked === 'page_root') continue;
            found.push({node, coverage: geometry.coverage, z: Number.isFinite(z) ? z : 0,
                        blocked, buttons: closeButtons(node)});
            if (found.length >= 64) break;
        }
        return found.sort((a, b) => b.z - a.z || b.coverage - a.coverage);
    };
    const saveStyle = node => {
        if (styles.has(node)) return true;
        if (styles.size >= 96) return false;
        styles.set(node, node.getAttribute('style')); return true;
    };
    const saveScroll = node => {
        if (windowScroll === null) windowScroll = {x: scrollX, y: scrollY};
        let count = 0;
        for (let parent = node.parentElement; parent; parent = parent.parentElement, count++) {
            if (count >= 96 || (!scrolls.has(parent) && scrolls.size >= 96)) return false;
            if (!scrolls.has(parent)) scrolls.set(parent, {x: parent.scrollLeft, y: parent.scrollTop});
        }
        return true;
    };
    const changed = () => styles.size > 0 || windowScroll !== null;
    const scan = () => {
        const found = collect();
        selected = found.length ? found[0].node : null;
        if (!found.length) return {code: 'none', count: 0, changed: changed(), can_hide: false};
        const item = found[0], button = item.buttons[0];
        if (!saveStyle(item.node) || (button && !saveStyle(button))) return {code: 'limit', can_hide: false, changed: changed()};
        item.node.style.setProperty('outline', '2px solid #fb7185', 'important');
        if (button) {
            const canScroll = saveScroll(button);
            button.style.setProperty('outline', '3px solid #f59e0b', 'important');
            button.style.setProperty('outline-offset', '3px', 'important');
            if (canScroll) button.scrollIntoView({block: 'center', inline: 'center', behavior: 'instant'});
        }
        const closeVisible = Boolean(button && rect(button).inside);
        return {code: button ? 'close_found' : item.blocked || 'overlay_found', count: found.length,
                coverage: item.coverage, large: item.coverage > 0.6, close_visible: closeVisible,
                can_hide: !item.blocked && !closeVisible, changed: changed()};
    };
    const hide = () => {
        if (!selected || !selected.isConnected || !visible(selected)) return {code: 'stale', can_hide: false, changed: changed()};
        const blocked = reason(selected);
        if (blocked) return {code: blocked, can_hide: false, changed: changed()};
        if (closeButtons(selected).some(button => rect(button).inside)) return {code: 'close_available', can_hide: false, changed: changed()};
        const roots = [];
        for (const node of [document.documentElement, document.body]) {
            if (!node) continue;
            const style = getComputedStyle(node);
            if (['hidden', 'clip'].includes(style.overflowX) || ['hidden', 'clip'].includes(style.overflowY)) {
                roots.push(node);
            }
        }
        if (!saveStyle(selected) || roots.some(node => !saveStyle(node))) return {code: 'limit', can_hide: false, changed: changed()};
        saveScroll(selected);
        selected.style.setProperty('display', 'none', 'important');
        for (const node of roots) node.style.setProperty('overflow', 'auto', 'important');
        return {code: 'hidden', can_hide: false, changed: changed()};
    };
    const undo = () => {
        const count = styles.size;
        for (const [node, position] of scrolls) {
            if (node.isConnected) node.scrollTo({left: position.x, top: position.y, behavior: 'instant'});
        }
        if (windowScroll !== null) window.scrollTo({left: windowScroll.x, top: windowScroll.y, behavior: 'instant'});
        // Chromium serializes CSSOM edits lazily. Flush the pending CSS value
        // before removing an attribute that was originally absent.
        for (const [node, value] of styles) {
            if (value === null) {
                node.style.cssText = '';
                node.getAttribute('style');
                node.removeAttribute('style');
            } else node.setAttribute('style', value);
        }
        styles.clear(); scrolls.clear(); windowScroll = null; selected = null;
        return {code: 'restored', restored: count, can_hide: false, changed: false};
    };
    const inspect = () => {
        const found = collect();
        const root = document.documentElement;
        const outside = found.reduce((count, item) => count + Number(item.buttons.length > 0 && !item.buttons.some(button => rect(button).inside)), 0);
        const unclosable = found.some(item => item.coverage > 0.6 && !item.buttons.length);
        const overflow = Math.max(0, (root ? root.scrollWidth : 0) - innerWidth);
        return {viewport_width: innerWidth, viewport_height: innerHeight, horizontal_overflow: overflow,
                close_outside: outside, overlay_count: found.length, usable: overflow <= 8 && outside === 0 && !unclosable};
    };
    const api = Object.freeze({scan, hide, undo, inspect});
    Object.defineProperty(window, slot, {value: api, configurable: true});
    return api;
})()
"""


# Qt 的 runJavaScript 对嵌套普通对象并非总能转换为 QVariantMap，显式 JSON。
OVERLAY_SCAN_SCRIPT = 'JSON.stringify(' + _PAGE_TOOLS + '.scan())'
OVERLAY_HIDE_SCRIPT = 'JSON.stringify(' + _PAGE_TOOLS + '.hide())'
PAGE_UNDO_SCRIPT = 'JSON.stringify(' + _PAGE_TOOLS + '.undo())'
PAGE_FIT_METRICS_SCRIPT = 'JSON.stringify(' + _PAGE_TOOLS + '.inspect())'
