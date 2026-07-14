"""
浏览器管理模块 - 基于 camoufox（Python）管理浏览器实例
使用 camoufox Python 包自带的浏览器二进制，无需执行 playwright install。

安装方式：
  pip install camoufox
  python -m camoufox fetch       # 下载 Camoufox 浏览器二进制
"""

import asyncio
import os
import time
from pathlib import Path

import logging

logger = logging.getLogger(__name__)


class BrowserManager:
    """管理 Camoufox 浏览器的生命周期"""

    def __init__(self, data_dir: str, headless: bool = True, page_load_timeout: int = 30):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.page_load_timeout = page_load_timeout * 1000  # 转毫秒

        self._context = None
        self._page = None
        self._cm = None  # AsyncCamoufox 上下文管理器
        self._lock = asyncio.Lock()
        self._last_activity = 0.0  # 最后活动时间戳

    @property
    def page(self):
        return self._page

    @property
    def is_running(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    def touch(self):
        """更新最后活动时间戳"""
        self._last_activity = time.time()

    @property
    def idle_seconds(self) -> float:
        """返回浏览器空闲时间（秒），未启动时返回 0"""
        if not self.is_running or self._last_activity == 0.0:
            return 0.0
        return time.time() - self._last_activity

    async def launch(self):
        """启动 Camoufox 浏览器"""
        async with self._lock:
            if self.is_running:
                return self._page

            logger.info("正在启动 Camoufox 浏览器...")

            try:
                from camoufox.async_api import AsyncCamoufox

                # persistent_context=True 使其返回 BrowserContext
                # user_data_dir 保持登录会话在浏览器重启后不丢失
                user_data_dir = str(self.data_dir / "browser_profile")
                os.makedirs(user_data_dir, exist_ok=True)

                self._cm = AsyncCamoufox(
                    headless=self.headless,
                    persistent_context=True,
                    user_data_dir=user_data_dir,
                    block_webrtc=True,
                    i_know_what_im_doing=True,
                    viewport={"width": 1366, "height": 768},
                    config={
                        # 固定 DPR，防止随机缩放
                        "window.devicePixelRatio": 1.0,
                        # 固定窗口视口尺寸，禁用 BrowserForge 随机化
                        "window.innerWidth": 1366,
                        "window.innerHeight": 768,
                        "window.outerWidth": 1366,
                        "window.outerHeight": 848,
                        # 固定屏幕尺寸
                        "screen.width": 1920,
                        "screen.height": 1080,
                        "screen.availWidth": 1920,
                        "screen.availHeight": 1040,
                        "screen.availTop": 0,
                        "screen.availLeft": 0,
                        # 固定窗口位置
                        "window.screenX": 0,
                        "window.screenY": 0,
                    },
                    firefox_user_prefs={
                        "layout.css.devPixelsPerPx": "1.0",
                        "layout.css.dpi": 96,
                    },
                )
                self._context = await self._cm.__aenter__()
                logger.info("Camoufox 浏览器已启动")

            except ImportError as e:
                logger.error(f"导入 camoufox 失败: {e}")
                raise RuntimeError(f"缺少依赖: {e}")
            except Exception as e:
                err_msg = str(e)
                if "fetch" in err_msg.lower() or "not found" in err_msg.lower() or "executable" in err_msg.lower():
                    logger.error(
                        f"Camoufox 浏览器二进制未下载。请执行: python -m camoufox fetch\n原始错误: {e}"
                    )
                    raise RuntimeError(
                        "Camoufox 浏览器二进制未下载。请在终端执行:\n"
                        "python -m camoufox fetch"
                    )
                raise

            # 获取或创建 Page
            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = await self._context.new_page()

            self._page.set_default_timeout(self.page_load_timeout)

            # 固定视口大小，所有坐标以此为基准
            await self._page.set_viewport_size({"width": 1366, "height": 768})

            self.touch()
            logger.info("Camoufox 浏览器启动完成")
            return self._page

    async def shutdown(self):
        """关闭浏览器"""
        async with self._lock:
            self._page = None

            if self._cm:
                try:
                    await self._cm.__aexit__(None, None, None)
                    logger.info("Camoufox 浏览器已关闭")
                except Exception as e:
                    logger.warning(f"关闭浏览器时出错: {e}")
                finally:
                    self._cm = None
                    self._context = None

    async def navigate(self, url: str) -> bool:
        """导航到指定 URL"""
        if not self.is_running:
            return False
        self.touch()
        try:
            await self._page.goto(url, wait_until="domcontentloaded",
                                  timeout=self.page_load_timeout)
            return True
        except Exception as e:
            logger.error(f"导航到 {url} 失败: {e}")
            return False

    async def screenshot(self, quality: int = 60) -> bytes:
        """截取当前页面的截图，始终输出与视口一致的 CSS 像素分辨率"""
        if not self.is_running:
            return b""
        try:
            return await self._page.screenshot(
                type="jpeg", quality=quality, scale="css")
        except Exception:
            return b""

    async def click(self, x: float, y: float):
        """在指定坐标点击"""
        if not self.is_running:
            return
        self.touch()
        try:
            await self._page.mouse.click(x, y)
        except Exception as e:
            logger.warning(f"点击 ({x}, {y}) 失败: {e}")

    async def type_text(self, text: str):
        """输入文本"""
        if not self.is_running:
            return
        self.touch()
        try:
            await self._page.keyboard.type(text, delay=50)
        except Exception as e:
            logger.warning(f"输入文本失败: {e}")

    async def press_key(self, key: str):
        """按下按键"""
        if not self.is_running:
            return
        self.touch()
        try:
            await self._page.keyboard.press(key)
        except Exception as e:
            logger.warning(f"按键 {key} 失败: {e}")

    async def mouse_down(self, x: float, y: float):
        """鼠标按下"""
        if not self.is_running:
            return
        self.touch()
        try:
            await self._page.mouse.move(x, y)
            await self._page.mouse.down()
        except Exception as e:
            logger.warning(f"鼠标按下 ({x}, {y}) 失败: {e}")

    async def mouse_move(self, x: float, y: float):
        """鼠标移动"""
        if not self.is_running:
            return
        self.touch()
        try:
            await self._page.mouse.move(x, y)
        except Exception as e:
            logger.warning(f"鼠标移动 ({x}, {y}) 失败: {e}")

    async def mouse_up(self, x: float, y: float):
        """鼠标释放"""
        if not self.is_running:
            return
        self.touch()
        try:
            await self._page.mouse.move(x, y)
            await self._page.mouse.up()
        except Exception as e:
            logger.warning(f"鼠标释放 ({x}, {y}) 失败: {e}")

    async def scroll(self, x: float, y: float, delta_y: float):
        """滚动页面"""
        if not self.is_running:
            return
        self.touch()
        try:
            await self._page.mouse.wheel(0, delta_y)
        except Exception as e:
            logger.warning(f"滚动失败: {e}")

    async def get_current_url(self) -> str:
        """获取当前页面URL"""
        if not self.is_running:
            return ""
        try:
            return self._page.url
        except Exception:
            return ""

    async def get_element_at(self, x: float, y: float) -> dict | None:
        """获取指定坐标的元素信息（用于录制时生成 CSS 选择器）"""
        if not self.is_running:
            return None
        try:
            result = await self._page.evaluate("""
                ({x, y}) => {
                    const el = document.elementFromPoint(x, y);
                    if (!el) return null;

                    function getSelector(element) {
                        if (element.id) return '#' + CSS.escape(element.id);

                        for (const attr of element.attributes) {
                            if (attr.name.startsWith('data-') && attr.value) {
                                return '[' + attr.name + '="' + CSS.escape(attr.value) + '"]';
                            }
                        }

                        const parts = [];
                        let current = element;
                        while (current && current !== document.body) {
                            let sel = current.tagName.toLowerCase();
                            if (current.className && typeof current.className === 'string') {
                                const cls = current.className.trim().split(/\\s+/)
                                    .filter(c => c && !c.match(/^[0-9]/)).slice(0, 2);
                                if (cls.length > 0) sel += '.' + cls.join('.');
                            }
                            const parent = current.parentElement;
                            if (parent) {
                                const siblings = Array.from(parent.children)
                                    .filter(c => c.tagName === current.tagName);
                                if (siblings.length > 1) {
                                    sel += ':nth-child(' +
                                        (Array.from(parent.children).indexOf(current) + 1) + ')';
                                }
                            }
                            parts.unshift(sel);
                            current = parent;
                        }
                        return parts.join(' > ');
                    }

                    const selector = getSelector(el);
                    return {
                        tag: el.tagName.toLowerCase(),
                        text: (el.textContent || '').trim().substring(0, 100),
                        selector: selector,
                        href: el.href || null,
                        type: el.type || null,
                        className: el.className || '',
                    };
                }
            """, {"x": x, "y": y})
            return result
        except Exception as e:
            logger.warning(f"获取元素信息失败: {e}")
            return None
