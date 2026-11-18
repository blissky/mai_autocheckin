// 仅注入“降低占用”页面；切换模式时重建页面，恢复原生行为。
(() => {
  const nativeTimeout = window.setTimeout.bind(window);
  const nativeClearTimeout = window.clearTimeout.bind(window);
  const nativeInterval = window.setInterval.bind(window);
  const nativeFrame = window.requestAnimationFrame.bind(window);
  const nativeCancelFrame = window.cancelAnimationFrame.bind(window);
  const callbacks = new Map();
  let nextId = 0;
  let timer = null;
  let frame = null;
  let lastFrame = 0;

  function scheduleFrame() {
    if (timer !== null || frame !== null) return;
    timer = nativeTimeout(() => {
      timer = null;
      frame = nativeFrame(timestamp => {
        frame = null;
        lastFrame = timestamp;
        const batch = new Map(callbacks);
        for (const [id, callback] of batch) {
          if (!callbacks.delete(id)) continue;
          try {
            callback(timestamp);
          } catch (error) {
            nativeTimeout(() => { throw error; }, 0);
          }
        }
        if (callbacks.size) scheduleFrame();
      });
    }, Math.max(0, 100 - (performance.now() - lastFrame)));
  }

  // 同一帧内的回调共用一个调度器，限制到约 10 FPS。
  window.requestAnimationFrame = callback => {
    if (typeof callback !== 'function') throw new TypeError('callback must be a function');
    const id = ++nextId;
    callbacks.set(id, callback);
    scheduleFrame();
    return id;
  };
  window.cancelAnimationFrame = id => {
    callbacks.delete(id);
    if (!callbacks.size) {
      if (timer !== null) nativeClearTimeout(timer);
      if (frame !== null) nativeCancelFrame(frame);
      timer = null;
      frame = null;
    }
  };
  // 保留一次性 setTimeout，避免阻碍页面初始化；缩减高频周期任务。
  window.setInterval = (handler, delay = 0, ...args) =>
    nativeInterval(handler, Math.max(250, Number(delay) || 0), ...args);

  const style = document.createElement('style');
  style.textContent = `
    *, *::before, *::after {
      animation-duration: 0.001ms !important;
      animation-delay: 0s !important;
      animation-iteration-count: 1 !important;
      transition-duration: 0.001ms !important;
      transition-delay: 0s !important;
      scroll-behavior: auto !important;
      box-shadow: none !important;
      text-shadow: none !important;
      backdrop-filter: none !important;
    }
  `;
  function installStyle() {
    if (!document.documentElement) return false;
    document.documentElement.appendChild(style);
    return true;
  }
  if (!installStyle()) {
    const observer = new MutationObserver(() => {
      if (installStyle()) observer.disconnect();
    });
    observer.observe(document, { childList: true });
  }
  // 兼顾通过 blob / MediaSource 播放、未经过 media 请求拦截的内容。
  document.addEventListener('play', event => {
    if (event.target instanceof HTMLMediaElement) event.target.pause();
  }, true);
})();
