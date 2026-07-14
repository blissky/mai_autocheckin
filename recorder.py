"""
签到操作录制与回放模块
- 录制模式：捕获用户的点击、输入、导航操作，保存为 JSON 动作序列
- 回放模式：按顺序重放录制的动作序列
"""

import asyncio
import base64
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import logging

logger = logging.getLogger(__name__)


@dataclass
class Action:
    """单个操作动作"""
    type: str  # click, type, press_key, navigate, scroll, wait
    timestamp: float = 0.0
    x: float = 0.0
    y: float = 0.0
    text: str = ""
    selector: str = ""
    url: str = ""
    key: str = ""
    delta_y: float = 0.0
    delay: float = 0.0  # 与上一个动作的时间间隔（秒）
    element_info: dict = field(default_factory=dict)


@dataclass
class SiteConfig:
    """站点签到配置"""
    name: str
    url: str
    actions: list = field(default_factory=list)  # List[dict] (Action 的序列化)
    enabled: bool = True
    last_checkin: str = ""  # 最后签到时间
    last_result: str = ""  # 最后签到结果
    vision_region: dict = field(default_factory=dict)  # {x, y, width, height}
    vision_keywords: str = ""  # 识图关键词/正则表达式


class Recorder:
    """操作录制器"""

    def __init__(self):
        self.is_recording = False
        self.actions: list[Action] = []
        self._start_time: float = 0.0
        self._last_action_time: float = 0.0

    def start(self):
        """开始录制"""
        self.is_recording = True
        self.actions = []
        self._start_time = time.time()
        self._last_action_time = self._start_time
        logger.info("开始录制签到操作")

    def stop(self) -> list[dict]:
        """停止录制并返回动作列表"""
        self.is_recording = False
        result = [asdict(a) for a in self.actions]
        logger.info(f"录制结束，共 {len(result)} 个操作")
        return result

    def record_action(self, action_type: str, **kwargs):
        """记录一个操作"""
        if not self.is_recording:
            return

        now = time.time()
        delay = now - self._last_action_time
        self._last_action_time = now

        action = Action(
            type=action_type,
            timestamp=now,
            delay=round(delay, 2),
            **kwargs,
        )
        self.actions.append(action)

    def record_click(self, x: float, y: float, selector: str = "", element_info: dict = None):
        self.record_action("click", x=x, y=y, selector=selector,
                           element_info=element_info or {})

    def record_type(self, text: str):
        self.record_action("type", text=text)

    def record_key(self, key: str):
        self.record_action("press_key", key=key)

    def record_navigate(self, url: str):
        self.record_action("navigate", url=url)

    def record_scroll(self, x: float, y: float, delta_y: float):
        self.record_action("scroll", x=x, y=y, delta_y=delta_y)

    def record_drag(self, from_x: float, from_y: float, to_x: float, to_y: float):
        self.record_action("drag", x=from_x, y=from_y,
                           element_info={"toX": to_x, "toY": to_y})

    def record_wait(self, seconds: float):
        self.record_action("wait", delay=seconds)


class CheckinManager:
    """签到管理器 - 管理站点列表和执行签到"""

    def __init__(self, data_dir: str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.config_file = self.data_dir / "sites.json"
        self.legacy_config_file = self.data_dir / "forums.json"
        self.sites: list[SiteConfig] = []
        self._load_sites()

    def _load_sites(self):
        """加载站点配置，兼容旧版 forums.json"""
        config_path = None
        if self.config_file.exists():
            config_path = self.config_file
        elif self.legacy_config_file.exists():
            config_path = self.legacy_config_file

        if not config_path:
            self.sites = []
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.sites = [SiteConfig(**item) for item in data]
            logger.info(f"已加载 {len(self.sites)} 个站点配置")
            if config_path == self.legacy_config_file:
                logger.info("检测到旧版 forums.json，已自动迁移为 sites.json")
                self._save_sites()
        except Exception as e:
            logger.error(f"加载站点配置失败: {e}")
            self.sites = []

    def _save_sites(self):
        """保存站点配置"""
        try:
            data = []
            for f in self.sites:
                d = {
                    "name": f.name,
                    "url": f.url,
                    "actions": f.actions,
                    "enabled": f.enabled,
                    "last_checkin": f.last_checkin,
                    "last_result": f.last_result,
                    "vision_region": f.vision_region,
                    "vision_keywords": f.vision_keywords,
                }
                data.append(d)
            with open(self.config_file, "w", encoding="utf-8") as fp:
                json.dump(data, fp, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存站点配置失败: {e}")

    def add_site(self, name: str, url: str) -> SiteConfig:
        """添加站点"""
        site = SiteConfig(name=name, url=url)
        self.sites.append(site)
        self._save_sites()
        return site

    def remove_site(self, name: str) -> bool:
        """移除站点"""
        for i, f in enumerate(self.sites):
            if f.name == name:
                self.sites.pop(i)
                self._save_sites()
                return True
        return False

    def get_site(self, name: str) -> Optional[SiteConfig]:
        """获取指定站点"""
        for f in self.sites:
            if f.name == name:
                return f
        return None

    def update_site_actions(self, name: str, actions: list[dict]):
        """更新站点的签到操作"""
        site = self.get_site(name)
        if site:
            site.actions = actions
            self._save_sites()

    def update_checkin_result(self, name: str, result: str):
        """更新签到结果"""
        site = self.get_site(name)
        if site:
            from datetime import datetime
            site.last_checkin = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            site.last_result = result
            self._save_sites()

    def get_enabled_sites(self) -> list[SiteConfig]:
        """获取所有启用的站点"""
        return [f for f in self.sites if f.enabled]

    def get_all_sites(self) -> list[dict]:
        """获取所有站点的摘要信息"""
        result = []
        for f in self.sites:
            result.append({
                "name": f.name,
                "url": f.url,
                "enabled": f.enabled,
                "has_actions": len(f.actions) > 0,
                "action_count": len(f.actions),
                "last_checkin": f.last_checkin,
                "last_result": f.last_result,
                "vision_region": f.vision_region,
                "vision_keywords": f.vision_keywords,
            })
        return result

    def toggle_site(self, name: str) -> bool:
        """切换站点启用/禁用状态"""
        site = self.get_site(name)
        if site:
            site.enabled = not site.enabled
            self._save_sites()
            return True
        return False

    def update_site_vision(self, name: str, region: dict, keywords: str):
        """更新站点的识图选区和关键词"""
        site = self.get_site(name)
        if site:
            site.vision_region = region
            site.vision_keywords = keywords
            self._save_sites()

    @property
    def forums(self) -> list[SiteConfig]:
        """兼容旧代码：返回站点列表"""
        return self.sites

    def add_forum(self, name: str, url: str) -> SiteConfig:
        """兼容旧代码：添加站点"""
        return self.add_site(name, url)

    def remove_forum(self, name: str) -> bool:
        """兼容旧代码：移除站点"""
        return self.remove_site(name)

    def get_forum(self, name: str) -> Optional[SiteConfig]:
        """兼容旧代码：获取站点"""
        return self.get_site(name)

    def update_forum_actions(self, name: str, actions: list[dict]):
        """兼容旧代码：更新站点的签到操作"""
        self.update_site_actions(name, actions)

    def get_enabled_forums(self) -> list[SiteConfig]:
        """兼容旧代码：获取所有启用的站点"""
        return self.get_enabled_sites()

    def get_all_forums(self) -> list[dict]:
        """兼容旧代码：获取所有站点的摘要信息"""
        return self.get_all_sites()

    def toggle_forum(self, name: str) -> bool:
        """兼容旧代码：切换站点启用/禁用状态"""
        return self.toggle_site(name)

    def update_forum_vision(self, name: str, region: dict, keywords: str):
        """兼容旧代码：更新站点的识图选区和关键词"""
        self.update_site_vision(name, region, keywords)


async def run_checkin(browser_manager, site: SiteConfig, action_delay: int = 1000,
                      vision_llm=None,
                      use_vision_check: bool = False,
                      checkin_wait: int = 5) -> str:
    """
    执行单个站点的签到操作

    Returns:
        str: "success" 或 "already_checked_in" 或 错误描述
    """
    page = browser_manager.page
    if not page or page.is_closed():
        return "浏览器未启动"

    if not site.actions:
        return "未录制签到操作"

    try:
        # 导航到站点
        logger.info(f"正在签到: {site.name} ({site.url})")
        browser_manager.touch()
        try:
            await page.goto(site.url, wait_until="domcontentloaded",
                            timeout=browser_manager.page_load_timeout)
        except Exception as e:
            return f"页面加载失败: {str(e)[:50]}"
        finally:
            browser_manager.touch()

        # 等待页面完全加载
        if checkin_wait > 0:
            logger.info(f"[{site.name}] 等待页面加载 {checkin_wait} 秒...")
            await asyncio.sleep(checkin_wait)

        # 签到前识图预检：如果已签到则跳过操作
        if (use_vision_check and vision_llm and
                site.vision_region and site.vision_keywords):
            try:
                logger.info(f"[{site.name}] 执行签到前识图预检...")
                browser_manager.touch()
                pre_result = await vision_check(
                    browser_manager, site, vision_llm)
                if pre_result["success"]:
                    logger.info(
                        f"[{site.name}] 识图预检发现已签到: {pre_result['matched']}，跳过操作")
                    return "already_checked_in"
                else:
                    reason = pre_result.get("error") or "关键词未匹配"
                    logger.info(f"[{site.name}] 识图预检未检测到已签到 ({reason})，继续执行签到")
            except Exception as e:
                logger.warning(f"[{site.name}] 识图预检出错，跳过预检继续签到: {e}")
            finally:
                browser_manager.touch()

        # 按顺序执行录制的操作
        for i, action_dict in enumerate(site.actions):
            action_type = action_dict.get("type", "")
            delay = action_dict.get("delay", action_delay / 1000)
            # 使用录制的延迟或配置的最小延迟
            wait_time = max(delay, action_delay / 1000)
            await asyncio.sleep(wait_time)

            browser_manager.touch()
            try:
                if action_type == "click":
                    selector = action_dict.get("selector", "")
                    x = action_dict.get("x", 0)
                    y = action_dict.get("y", 0)

                    if selector:
                        # 优先使用选择器（更稳定）
                        try:
                            el = page.locator(selector).first
                            await el.wait_for(state="visible", timeout=5000)
                            await el.click(timeout=5000)
                        except Exception:
                            # 选择器失败则用坐标
                            await page.mouse.click(x, y)
                    else:
                        await page.mouse.click(x, y)

                elif action_type == "type":
                    text = action_dict.get("text", "")
                    await page.keyboard.type(text, delay=50)

                elif action_type == "press_key":
                    key = action_dict.get("key", "")
                    await page.keyboard.press(key)

                elif action_type == "navigate":
                    url = action_dict.get("url", "")
                    await page.goto(url, wait_until="domcontentloaded",
                                    timeout=browser_manager.page_load_timeout)

                elif action_type == "scroll":
                    delta_y = action_dict.get("delta_y", 0)
                    await page.mouse.wheel(0, delta_y)

                elif action_type == "drag":
                    fx = action_dict.get("x", 0)
                    fy = action_dict.get("y", 0)
                    ei = action_dict.get("element_info", {})
                    tx = ei.get("toX", action_dict.get("toX", fx))
                    ty = ei.get("toY", action_dict.get("toY", fy))
                    await page.mouse.move(fx, fy)
                    await page.mouse.down()
                    steps = max(int(((tx-fx)**2+(ty-fy)**2)**0.5/20), 5)
                    for s in range(1, steps+1):
                        mx = fx + (tx-fx)*s/steps
                        my = fy + (ty-fy)*s/steps
                        await page.mouse.move(mx, my)
                        await asyncio.sleep(0.01)
                    await page.mouse.move(tx, ty)
                    await page.mouse.up()

                elif action_type == "wait":
                    extra_wait = action_dict.get("delay", 1)
                    await asyncio.sleep(extra_wait)

            except Exception as e:
                logger.warning(f"执行操作 {i+1}/{len(site.actions)} ({action_type}) 失败: {e}")
                # 继续执行后续操作，不中断
            finally:
                browser_manager.touch()

        browser_manager.touch()
        logger.info(f"签到完成: {site.name}")
        return "success"

    except Exception as e:
        error_msg = f"签到异常: {str(e)[:100]}"
        logger.error(f"{site.name} {error_msg}")
        return error_msg


async def execute_site_checkin(browser_manager, checkin_manager: CheckinManager,
                               site: SiteConfig, action_delay: int = 1000,
                               vision_llm=None,
                               use_vision_check: bool = False,
                               checkin_wait: int = 5) -> dict:
    """
    执行单个站点签到，并统一处理签到结果判定。

    Returns:
        dict: {
            "success": bool,
            "result": str,
            "stored_result": str,
            "raw_result": str,
            "vision_image": str,
            "vision_text": str,
            "vision_matched": str,
        }
    """
    raw_result = await run_checkin(
        browser_manager, site, action_delay,
        vision_llm=vision_llm,
        use_vision_check=use_vision_check,
        checkin_wait=checkin_wait,
    )

    vision_image = ""
    vision_text = ""
    vision_matched = ""

    if (use_vision_check and vision_llm and
            site.vision_region and site.vision_keywords):
        browser_manager.touch()
        vr = await vision_check(
            browser_manager, site, vision_llm)
        vision_image = vr.get("image_b64", "")
        vision_text = vr.get("llm_text", "")
        vision_matched = vr.get("matched", "")

        if vr["success"]:
            if raw_result == "already_checked_in":
                result = f"已签到，跳过；识图匹配: {vision_matched}"
                stored_result = f"成功 ({result})"
            else:
                result = f"识图验证: {vision_matched}"
                stored_result = f"成功 ({result})"
            success = True
        else:
            reason = vr.get("error") or "未匹配关键词"
            result = f"识图验证未匹配: {reason}"
            stored_result = f"失败: {result}"
            success = False
    else:
        success = raw_result in ("success", "already_checked_in")
        if raw_result == "already_checked_in":
            result = "已签到，跳过"
            stored_result = "成功 (已签到，跳过)"
        elif raw_result == "success":
            result = "成功"
            stored_result = "成功"
        else:
            result = raw_result
            stored_result = f"失败: {raw_result}"

    checkin_manager.update_checkin_result(site.name, stored_result)
    return {
        "success": success,
        "result": result,
        "stored_result": stored_result,
        "raw_result": raw_result,
        "vision_image": vision_image,
        "vision_text": vision_text,
        "vision_matched": vision_matched,
    }


async def run_all_checkins(browser_manager, checkin_manager: CheckinManager,
                           action_delay: int = 1000,
                           vision_llm=None,
                           use_vision_check: bool = False,
                           checkin_wait: int = 5) -> dict:
    """
    执行所有启用站点的签到

    Returns:
        dict: {"success": [...], "failed": [{"name": ..., "error": ...}]}
    """
    sites = checkin_manager.get_enabled_sites()
    if not sites:
        return {"success": [], "failed": [], "message": "没有启用的站点"}

    # 确保浏览器已启动
    if not browser_manager.is_running:
        try:
            await browser_manager.launch()
        except Exception as e:
            return {"success": [], "failed": [], "message": f"浏览器启动失败: {e}"}

    results = {"success": [], "failed": [], "vision_images": {}}

    for site in sites:
        outcome = await execute_site_checkin(
            browser_manager, checkin_manager, site, action_delay,
            vision_llm=vision_llm,
            use_vision_check=use_vision_check,
            checkin_wait=checkin_wait,
        )

        if outcome["vision_image"]:
            results["vision_images"][site.name] = {
                "image": outcome["vision_image"],
                "text": outcome["vision_text"],
                "matched": outcome["vision_matched"],
            }

        if outcome["success"]:
            results["success"].append(site.name)
        else:
            results["failed"].append({"name": site.name, "error": outcome["result"]})

    return results


import re
import base64


async def vision_check(browser_manager, site: SiteConfig,
                       vision_llm) -> dict:
    """
    使用多模态大模型识别签到结果

    Args:
        browser_manager: 浏览器管理器
        site: 站点配置（含 vision_region 和 vision_keywords）
        vision_llm: 异步回调 vision_llm(img_b64: str, prompt: str) -> str，
                    由插件入口注入，负责调用宿主的多模态 LLM 并返回识别文本

    Returns:
        dict: {"success": bool, "llm_text": str, "matched": str, "image_b64": str}
    """
    region = site.vision_region
    if not region or not region.get("width") or not region.get("height"):
        return {"success": False, "llm_text": "", "matched": "", "error": "未设置识图选区", "image_b64": ""}

    page = browser_manager.page
    if not page or page.is_closed():
        return {"success": False, "llm_text": "", "matched": "", "error": "浏览器未启动", "image_b64": ""}

    if vision_llm is None:
        return {"success": False, "llm_text": "", "matched": "", "error": "未提供识图 LLM 回调", "image_b64": ""}

    try:
        # 截取指定区域的截图
        browser_manager.touch()
        clip = {
            "x": region["x"], "y": region["y"],
            "width": region["width"], "height": region["height"],
        }
        img_bytes = await page.screenshot(type="jpeg", quality=80, clip=clip, scale="css")
        img_b64 = base64.b64encode(img_bytes).decode()
        browser_manager.touch()

        prompt_text = (
            "请仔细查看这张截图，识别其中所有可见的文字内容。"
            "请直接逐行列出你看到的所有文字，不要遗漏任何文字，不要添加解释或分析。"
        )

        llm_text = await vision_llm(img_b64, prompt_text)
        llm_text = llm_text or ""

        # 关键词/正则匹配
        keywords = site.vision_keywords.strip()
        if not keywords:
            return {"success": True, "llm_text": llm_text, "matched": "",
                    "error": "未设置识图关键词，仅返回识别结果", "image_b64": img_b64}

        matched = ""
        try:
            pattern = re.compile(keywords)
            match = pattern.search(llm_text)
            if match:
                matched = match.group(0)
        except re.error:
            # 正则无效时当普通文本匹配
            if keywords in llm_text:
                matched = keywords

        return {
            "success": bool(matched),
            "llm_text": llm_text,
            "matched": matched,
            "error": "",
            "image_b64": img_b64,
        }

    except Exception as e:
        logger.error(f"识图验证失败: {e}")
        return {"success": False, "llm_text": "", "matched": "", "error": str(e), "image_b64": ""}
