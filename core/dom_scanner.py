"""内嵌页面的只读视频观察脚本；不点击页面、不读取 EME 密钥。"""


DOM_OBSERVER_SCRIPT = r"""
(() => {
    const slot = '__streamMediaDiscoveryObserver_v1';
    if (window[slot]) return;
    const records = new WeakMap();
    let elementSerial = 0;
    const eventNames = ['play', 'playing', 'pause', 'loadedmetadata', 'durationchange',
                        'seeking', 'seeked', 'ended', 'encrypted'];
    const record = video => {
        let item = records.get(video);
        if (!item) { item = {events: [], encrypted: false, id: 'media-' + (++elementSerial),
                            lastTime: null, lastSource: ''}; records.set(video, item); }
        return item;
    };
    for (const name of eventNames) {
        document.addEventListener(name, event => {
            const video = event.target;
            if (!video || !['VIDEO', 'AUDIO'].includes(video.tagName)) return;
            const item = record(video);
            item.events.push({type: name, timestamp: Date.now() / 1000});
            if (item.events.length > 48) item.events.splice(0, item.events.length - 48);
            if (name === 'encrypted') item.encrypted = true;
        }, true);
    }
    const snapshot = () => {
        const timestamp = Date.now() / 1000;
        const nodes = Array.from(document.querySelectorAll('video,audio')).slice(0, 96);
        const videos = nodes.map(video => {
            const item = record(video);
            const rectangle = video.getBoundingClientRect();
            const style = getComputedStyle(video);
            const width = Math.max(0, Math.min(rectangle.right, innerWidth) - Math.max(rectangle.left, 0));
            const height = Math.max(0, Math.min(rectangle.bottom, innerHeight) - Math.max(rectangle.top, 0));
            let shown = style.visibility !== 'hidden' && style.visibility !== 'collapse' &&
                        style.display !== 'none' && Number(style.opacity || 1) > 0;
            for (let parent = video.parentElement, depth = 0; parent && depth < 16; parent = parent.parentElement, depth++) {
                const ancestor = getComputedStyle(parent);
                if (ancestor.display === 'none' || ancestor.visibility === 'hidden' || Number(ancestor.opacity || 1) <= 0) shown = false;
            }
            const visible = width > 0 && height > 0 && shown;
            const currentTime = Number(video.currentTime) || 0;
            const progression = item.lastSource === video.currentSrc && item.lastTime !== null &&
                                currentTime > item.lastTime && currentTime - item.lastTime < 3;
            item.lastTime = currentTime; item.lastSource = video.currentSrc;
            const sources = Array.from(video.querySelectorAll('source')).slice(0, 32)
                .map(source => ({src: source.src || '', type: source.type || ''}));
            const current = sources.find(source => source.src === video.currentSrc);
            return {
                src: video.src || '', current_src: video.currentSrc || '', sources,
                subtitle_tracks: Array.from(video.querySelectorAll('track')).slice(0, 128)
                    .map(track => ({src: (track.src || '').slice(0, 65536),
                        language: (track.srclang || '').slice(0, 40), name: (track.label || '').slice(0, 256),
                        kind: track.kind || '', default: Boolean(track.default)})),
                tag: video.tagName, poster: video.poster || '',
                element_id: item.id, video_id: video.dataset.videoId || '',
                title: (video.getAttribute('title') || video.getAttribute('aria-label') ||
                        video.dataset.title || '').slice(0, 512),
                type: (current && current.type) || video.getAttribute('type') || '',
                width: Number(video.videoWidth) || 0, height: Number(video.videoHeight) || 0,
                duration: Number.isFinite(video.duration) ? video.duration : null,
                paused: Boolean(video.paused), ended: Boolean(video.ended), seeking: Boolean(video.seeking),
                current_time: currentTime, progressing: progression, ready_state: Number(video.readyState) || 0,
                focused: video === document.activeElement || video.contains(document.activeElement),
                visible, area: visible ? width * height : 0, is_main: false,
                media_keys: Boolean(video.mediaKeys), encrypted: Boolean(item.encrypted),
                events: item.events.filter(event => timestamp - event.timestamp <= 300).slice(-48)
            };
        });
        const eligible = videos.map((video, index) => ({video, index}))
            .filter(({video}) => video.visible && video.tag === 'VIDEO' && video.current_src && video.ready_state >= 1)
            .sort((a,b) => b.video.area - a.video.area);
        let main = -1;
        if (eligible.length === 1) {
            main = eligible[0].index;
        } else {
            for (const key of ['focused','progressing']) {
                const matching = eligible.filter(({video}) => video[key]);
                if (matching.length === 1) { main = matching[0].index; break; }
            }
            if (main < 0 && eligible.length > 1 && eligible[0].video.area >= eligible[1].video.area * 1.5) {
                main = eligible[0].index;
            }
        }
        if (main >= 0) videos[main].is_main = true;
        const next = document.getElementById('__NEXT_DATA__');
        const next_data = next && next.textContent.length <= 2097152 ? next.textContent : null;
        const performance_resources = performance.getEntriesByType('resource').slice(-128)
            .map(item => ({url: item.name.slice(0, 65536), type: item.initiatorType,
                           duration: item.duration, start_time: item.startTime}));
        return {page_url: location.href, page_title: document.title.slice(0, 512), frame_id: '', timestamp, videos, next_data,
                performance_resources};
    };
    Object.defineProperty(window, slot, {value: {snapshot}, configurable: true});
})();
"""


DOM_SNAPSHOT_SCRIPT = '(' + r"""
() => {
""" + DOM_OBSERVER_SCRIPT + r"""
    return window.__streamMediaDiscoveryObserver_v1.snapshot();
}
""" + ')()'
