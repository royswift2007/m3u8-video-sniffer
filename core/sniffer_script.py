
# JS 嗅探脚本：增强版 Hook 逻辑
# Hook XMLHttpRequest、fetch、<video> 标签、MutationObserver 来捕获动态加载的媒体资源

SNIFFER_JS = r"""
(function() {
    if (window._catcatch_hooked) return;
    window._catcatch_hooked = true;

    console.log("CatCatch Enhanced Sniffer Hook Loaded");
    
    // 已报告的 URL 集合 (去重)
    const reportedUrls = new Set();

    // 检查 URL 是否为感兴趣的媒体资源
    function checkAndReport(url, source, duration) {
        if (!url) return;
        
        try {
            // 转换为绝对 URL
            const absoluteUrl = new URL(url, window.location.href).href;
            const lowerUrl = absoluteUrl.toLowerCase();
            
            // 去重
            if (reportedUrls.has(absoluteUrl)) return;
            
            // 排除 data: URL
            if (lowerUrl.startsWith('data:')) return;
            
            // 排除常见非视频资源
            const skipPatterns = ['.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff', '.ttf'];
            if (skipPatterns.some(p => lowerUrl.includes(p))) return;

            // 特征匹配
            let isMedia = false;
            
            // 1. 常见流媒体后缀
            if (lowerUrl.includes('.m3u8') || lowerUrl.includes('.mpd')) {
                isMedia = true;
            }
            // 2. 常见视频后缀 (忽略参数)
            else {
                const urlNoParams = lowerUrl.split('?')[0];
                if (urlNoParams.match(/\.(mp4|flv|mkv|mov|m4v|f4v|3gp|webm|avi|wmv)$/)) {
                    isMedia = true;
                }
            }
            
            // 3. 关键字匹配 (某些动态流 URL)
            if (!isMedia) {
                const keywords = ['videoplayback', '/hls/', '/dash/', '/stream/', '/video/', 'master.m3u8', 'playlist.m3u8'];
                if (keywords.some(kw => lowerUrl.includes(kw))) {
                    isMedia = true;
                }
            }
            
            // 4. Blob URL 特殊处理 (记录但标记)
            if (lowerUrl.startsWith('blob:')) {
                // Blob URL 无法直接下载，暂时跳过
                return;
            }

            if (isMedia) {
                reportedUrls.add(absoluteUrl);
                // 通过 console.log 传回 Python, 格式: CATCATCH_DETECT:URL|DURATION|SOURCE
                const durationStr = duration && !isNaN(duration) && duration > 0 ? Math.round(duration) : '';
                console.log("CATCATCH_DETECT:" + absoluteUrl + "|" + durationStr + "|" + (source || 'Unknown'));
            }
        } catch (e) {
            // ignore invalid urls
        }
    }

    // === 1. Hook XMLHttpRequest ===
    const rawOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function(method, url) {
        checkAndReport(url, 'XHR', 0);
        return rawOpen.apply(this, arguments);
    };

    // === 2. Hook Fetch ===
    const rawFetch = window.fetch;
    window.fetch = function(input, init) {
        let url = input;
        if (input instanceof Request) {
            url = input.url;
        }
        checkAndReport(url, 'Fetch', 0);
        return rawFetch.apply(this, arguments);
    };
    
    // === 3. 监听 <video> 和 <source> 标签 ===
    function scanVideoElements(root) {
        try {
            // 扫描所有 video 标签
            const videos = root.querySelectorAll('video');
            videos.forEach(video => {
                const duration = video.duration;
                if (video.src) {
                    checkAndReport(video.src, 'VideoElement', duration);
                }
                if (video.currentSrc) {
                    checkAndReport(video.currentSrc, 'VideoCurrentSrc', duration);
                }
                // 扫描 source 子元素
                const sources = video.querySelectorAll('source');
                sources.forEach(source => {
                    if (source.src) {
                        checkAndReport(source.src, 'SourceElement', duration);
                    }
                });
            });
            
            // 单独扫描 source 标签 (可能在 audio 等元素中)
            const allSources = root.querySelectorAll('source');
            allSources.forEach(source => {
                const type = source.type || '';
                if (type.includes('video') || type.includes('mpegurl') || type.includes('mp4')) {
                    checkAndReport(source.src, 'SourceElement', 0);
                }
            });
        } catch (e) {}
    }
    
    // 初始扫描
    scanVideoElements(document);
    
    // === 4. MutationObserver 监听动态添加的元素 ===
    const observer = new MutationObserver((mutations) => {
        mutations.forEach(mutation => {
            // 新增节点
            mutation.addedNodes.forEach(node => {
                if (node.nodeType === Node.ELEMENT_NODE) {
                    // 直接是 video/source
                    if (node.tagName === 'VIDEO' || node.tagName === 'SOURCE') {
                        checkAndReport(node.src, 'MutationVideo', node.duration || 0);
                        if (node.tagName === 'VIDEO') {
                            scanVideoElements(node);
                        }
                    }
                    // 包含子元素
                    if (node.querySelectorAll) {
                        scanVideoElements(node);
                    }
                }
            });
            
            // 属性变化 (src 变更)
            if (mutation.type === 'attributes' && mutation.attributeName === 'src') {
                const target = mutation.target;
                if (target.tagName === 'VIDEO' || target.tagName === 'SOURCE') {
                    checkAndReport(target.src, 'SrcAttributeChange', target.duration || 0);
                }
            }
        });
    });
    
    observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['src']
    });
    
    // === 5. 监听 HTMLMediaElement 事件 ===
    function hookMediaElement(element) {
        if (element._catcatch_media_hooked) return;
        element._catcatch_media_hooked = true;
        
        // 监听 loadedmetadata 事件来获取准确的时长
        element.addEventListener('loadedmetadata', () => {
            if (element.currentSrc) {
                checkAndReport(element.currentSrc, 'MediaMetadata', element.duration);
            }
        });
        
        element.addEventListener('loadstart', () => {
            if (element.src) checkAndReport(element.src, 'MediaLoadStart', 0);
            if (element.currentSrc) checkAndReport(element.currentSrc, 'MediaLoadStart', 0);
        });
        
        element.addEventListener('play', () => {
            try { console.log("CATCATCH_PLAY:" + window.location.href); } catch (e) {}
            if (element.currentSrc) checkAndReport(element.currentSrc, 'MediaPlay', element.duration);
        });
        
        // 延迟检测时长（等待视频加载）
        element.addEventListener('durationchange', () => {
            if (element.currentSrc && element.duration > 0) {
                checkAndReport(element.currentSrc, 'DurationChange', element.duration);
            }
        });
    }
    
    // Hook 现有 video 元素
    document.querySelectorAll('video').forEach(hookMediaElement);
    
    // Hook 动态创建的 video 元素
    const originalCreateElement = document.createElement.bind(document);
    document.createElement = function(tagName) {
        const element = originalCreateElement(tagName);
        if (tagName.toLowerCase() === 'video') {
            setTimeout(() => hookMediaElement(element), 0);
        }
        return element;
    };
    
})();
"""
