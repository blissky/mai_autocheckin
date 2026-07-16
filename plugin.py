"""
MaiBot 自动签到插件
- 通过 WebUI 可视化操作 Camoufox/Firefox 浏览器登录网站
- 支持录制签到点击操作
- 支持 Cron 表达式定时批量签到
- 签到完成后通过机器人消息通知绑定的会话
"""

import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

try:
    from .browser_manager import BrowserManager
    from .recorder import (
        CheckinManager,
        Recorder,
        execute_site_checkin,
        run_all_checkins,
    )
    from .web_server import WebServer
except ImportError:
    from browser_manager import BrowserManager
    from recorder import (
        CheckinManager,
        Recorder,
        execute_site_checkin,
        run_all_checkins,
    )
    from web_server import WebServer

logger = logging.getLogger(__name__)


# ==================== Cron 解析 ====================

class CronRule:
    """解析并匹配单条 Cron 表达式（分 时 日 月 周）"""

    def __init__(self, expr: str):
        self.expr = expr.strip()
        parts = self.expr.split()
        if len(parts) != 5:
            raise ValueError(f"Cron 表达式必须为 5 个字段: {self.expr}")
        self._minute = self._parse_field(parts[0], 0, 59)
        self._hour = self._parse_field(parts[1], 0, 23)
        self._dom = self._parse_field(parts[2], 1, 31)
        self._month = self._parse_field(parts[3], 1, 12)
        self._dow = self._parse_field(parts[4], 0, 6)  # 0=周日, 1=周一 ... 6=周六

    @staticmethod
    def _parse_field(field: str, lo: int, hi: int) -> set[int]:
        """解析单个 cron 字段，返回匹配的整数集合"""
        result: set[int] = set()
        for part in field.split(","):
            part = part.strip()
            if not part:
                continue
            # */n 步进
            if part.startswith("*/"):
                step = int(part[2:])
                if step <= 0:
                    raise ValueError(f"步进值必须 > 0: {part}")
                result.update(range(lo, hi + 1, step))
            # n-m 范围（可带 /step）
            elif "-" in part or "/" in part:
                step = 1
                if "/" in part:
                    range_part, step_str = part.split("/", 1)
                    step = int(step_str)
                else:
                    range_part = part
                if "-" in range_part:
                    a, b = range_part.split("-", 1)
                    result.update(range(int(a), int(b) + 1, step))
                else:
                    result.update(range(int(range_part), hi + 1, step))
            elif part == "*":
                result.update(range(lo, hi + 1))
            else:
                result.add(int(part))
        return result

    def matches(self, dt: datetime) -> bool:
        """判断 datetime 是否匹配本条规则"""
        # 标准 cron: 0=Sun, 1=Mon ... 6=Sat
        # Python isoweekday(): 1=Mon ... 7=Sun → 转为: Sun=0, Mon=1 ... Sat=6
        dow = dt.isoweekday() % 7  # 1→1, 2→2, ... 6→6, 7→0
        return (
            dt.minute in self._minute
            and dt.hour in self._hour
            and dt.day in self._dom
            and dt.month in self._month
            and dow in self._dow
        )

    def __repr__(self):
        return f"CronRule({self.expr!r})"


def parse_cron_rules(text: str) -> list[CronRule]:
    """从多行文本中解析 cron 表达式列表，跳过空行和注释"""
    rules = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rules.append(CronRule(line))
        except (ValueError, IndexError) as e:
            logger.warning(f"无效的 Cron 表达式 '{line}': {e}")
    return rules


def format_cron_for_display(rules: list[CronRule]) -> str:
    """将 cron 规则列表格式化为可读字符串"""
    if not rules:
        return "未配置"
    return ", ".join(r.expr for r in rules)


# ==================== 配置模型 ====================

class WebUISectionConfig(PluginConfigBase):
    """WebUI 配置"""

    __ui_label__ = "WebUI"
    __ui_icon__ = "language"
    __ui_order__ = 0

    port: int = Field(
        default=9010, description="WebUI 控制面板端口",
        json_schema_extra={"title": "端口"},
    )
    host: str = Field(
        default="127.0.0.1",
        description="127.0.0.1 仅限本机访问，0.0.0.0 允许外部访问",
        json_schema_extra={"title": "监听地址"},
    )
    token: str = Field(
        default="sk-change-me",
        description="访问 WebUI 时需要输入，请务必修改默认值",
        json_schema_extra={"title": "登录密钥"},
    )
    session_timeout: int = Field(
        default=30, description="超时未操作后需要重新登录 WebUI",
        json_schema_extra={"title": "登录空闲超时（分钟）"},
    )
    trust_proxy: bool = Field(
        default=False,
        description="信任 X-Forwarded-For 请求头识别客户端 IP，仅在经反向代理访问时开启",
        json_schema_extra={"title": "信任反向代理"},
    )
    screenshot_interval: int = Field(
        default=500, description="WebUI 中浏览器画面的截图刷新间隔",
        json_schema_extra={"title": "画面刷新间隔（毫秒）"},
    )


class ScheduleSectionConfig(PluginConfigBase):
    """定时签到配置"""

    __ui_label__ = "定时计划"
    __ui_icon__ = "schedule"
    __ui_order__ = 1

    cron_rules: str = Field(
        default="30 8 * * *",
        description="每行一条 5 字段 Cron 表达式（分 时 日 月 周），# 开头为注释",
        json_schema_extra={"title": "签到计划"},
    )
    timezone: str = Field(
        default="Asia/Shanghai", description="定时签到使用的时区",
        json_schema_extra={"title": "时区"},
    )


class BrowserSectionConfig(PluginConfigBase):
    """浏览器配置"""

    __ui_label__ = "浏览器"
    __ui_icon__ = "public"
    __ui_order__ = 2

    headless: bool = Field(
        default=True, description="不显示浏览器窗口运行，服务器环境请保持开启",
        json_schema_extra={"title": "无头模式"},
    )
    page_load_timeout: int = Field(
        default=30, description="站点页面加载的最长等待时间",
        json_schema_extra={"title": "页面加载超时（秒）"},
    )
    action_delay: int = Field(
        default=1000, description="回放录制操作之间的最小间隔",
        json_schema_extra={"title": "操作间隔（毫秒）"},
    )
    checkin_wait: int = Field(
        default=5,
        description="站点导航完成后、识图预检与动作回放前的等待时间",
        json_schema_extra={"title": "签到前等待（秒）"},
    )
    idle_timeout: int = Field(
        default=10,
        description="浏览器空闲超过该时长后自动关闭，0 表示禁用",
        json_schema_extra={"title": "空闲自动关闭（分钟）"},
    )


class PluginSectionConfig(PluginConfigBase):
    """插件元信息（宿主硬性要求 [plugin] 节与 config_version 字段）"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 3

    config_version: str = Field(
        default="1.0.0",
        description="配置版本号（宿主要求的字段，勿手动修改）",
        json_schema_extra={"title": "配置版本"},
    )


class AutoCheckinConfig(PluginConfigBase):
    """插件完整配置"""

    webui: WebUISectionConfig = Field(default_factory=WebUISectionConfig)
    schedule: ScheduleSectionConfig = Field(default_factory=ScheduleSectionConfig)
    browser: BrowserSectionConfig = Field(default_factory=BrowserSectionConfig)
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)


# ==================== 插件主体 ====================

class AutoCheckinPlugin(MaiBotPlugin):
    """MaiBot 自动签到插件"""

    config_model = AutoCheckinConfig

    def __init__(self) -> None:
        super().__init__()
        self.data_dir: str = ""
        self.cron_rules: list[CronRule] = []
        self.timezone: ZoneInfo = ZoneInfo("Asia/Shanghai")
        self.browser_manager: BrowserManager | None = None
        self.recorder: Recorder | None = None
        self.checkin_manager: CheckinManager | None = None
        self.web_server: WebServer | None = None
        # 存储绑定的 stream_id 用于主动推送签到结果
        self._notify_targets: list[str] = []
        self._scheduler_task: asyncio.Task | None = None
        self._idle_check_task: asyncio.Task | None = None
        self._services_started = False
        # 识图验证开关：由宿主是否配置 vlm 任务决定（启动时检测）
        self._use_vision_check = False

    # ==================== 生命周期 ====================

    async def on_load(self) -> None:
        """插件加载 - 启动 WebUI 和定时任务"""
        self.data_dir = str(self.ctx.paths.data_dir)
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)

        await self._start_services()

    async def on_unload(self) -> None:
        """插件卸载/停用时清理资源"""
        await self._stop_services()
        self.ctx.logger.info("自动签到插件已停止")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载：重启内部服务以应用新配置"""
        del config_data
        del version
        if scope != "self":
            return

        self.ctx.logger.info("检测到插件配置更新，正在重启内部服务...")
        await self._stop_services()
        await self._start_services()

    async def _start_services(self) -> None:
        """按当前配置启动全部内部服务"""
        if self._services_started:
            return

        cfg = self.config
        self.cron_rules = parse_cron_rules(cfg.schedule.cron_rules)
        self.timezone = self._parse_timezone(cfg.schedule.timezone)

        # 检测宿主是否配置 vlm（视觉）任务，决定是否启用识图验证
        await self._detect_vision_support()

        # 确保 Python 包、系统依赖和 Camoufox 浏览器二进制已就绪
        self._ensure_python_deps()
        self._ensure_system_deps()
        await self._ensure_camoufox_binary()

        # 核心组件
        self.browser_manager = BrowserManager(
            data_dir=self.data_dir,
            headless=cfg.browser.headless,
            page_load_timeout=cfg.browser.page_load_timeout,
        )
        self.recorder = Recorder()
        self.checkin_manager = CheckinManager(data_dir=self.data_dir)
        self.web_server = WebServer(
            browser_manager=self.browser_manager,
            checkin_manager=self.checkin_manager,
            recorder=self.recorder,
            port=cfg.webui.port,
            screenshot_interval=cfg.webui.screenshot_interval,
            action_delay=cfg.browser.action_delay,
            webui_token=cfg.webui.token,
            webui_session_timeout=cfg.webui.session_timeout,
            webui_host=cfg.webui.host,
            webui_trust_proxy=cfg.webui.trust_proxy,
            vision_llm=self._vision_llm,
            use_vision_check=self._use_vision_check,
            checkin_wait=cfg.browser.checkin_wait,
        )

        self._load_notify_targets()

        try:
            await self.web_server.start()
            self.ctx.logger.info(
                f"自动签到 WebUI 已启动: http://{cfg.webui.host}:{cfg.webui.port}")
            if cfg.webui.host == "127.0.0.1":
                self.ctx.logger.info(
                    "WebUI 当前仅监听本机 127.0.0.1，如需远程访问请修改 webui.host 配置。")
            if cfg.webui.token == "sk-change-me":
                self.ctx.logger.warning(
                    "WebUI 正在使用默认登录密钥 sk-change-me，建议尽快在插件配置中修改。")
        except Exception as e:
            self.ctx.logger.error(f"WebUI 启动失败: {e}")

        # 启动定时签到调度器
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        self.ctx.logger.info(
            f"定时签到已设置: {format_cron_for_display(self.cron_rules)} (时区: {self.timezone})")

        # 启动浏览器空闲自动关闭检查
        if cfg.browser.idle_timeout > 0:
            self._idle_check_task = asyncio.create_task(self._idle_check_loop())
            self.ctx.logger.info(
                f"浏览器空闲自动关闭已启用: {cfg.browser.idle_timeout} 分钟")

        self._services_started = True

    async def _stop_services(self) -> None:
        """停止全部内部服务并清理资源"""
        for task_attr in ("_scheduler_task", "_idle_check_task"):
            task: asyncio.Task | None = getattr(self, task_attr)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                setattr(self, task_attr, None)

        if self.web_server:
            try:
                await self.web_server.stop()
            except Exception as e:
                self.ctx.logger.warning(f"停止 WebUI 时出错: {e}")
            self.web_server = None

        if self.browser_manager:
            try:
                await self.browser_manager.shutdown()
            except Exception as e:
                self.ctx.logger.warning(f"关闭浏览器时出错: {e}")
            self.browser_manager = None

        self._services_started = False

    # ==================== 识图 LLM 回调 ====================

    async def _detect_vision_support(self) -> None:
        """检测宿主是否配置 vlm（视觉）任务，自动决定识图验证开关"""
        try:
            models = await self.ctx.llm.get_available_models()
            self._use_vision_check = "vlm" in (models or [])
            if self._use_vision_check:
                self.ctx.logger.info("检测到宿主已配置 vlm 任务，识图验证已启用")
            else:
                self.ctx.logger.info(
                    f"宿主未配置 vlm 任务，识图验证已禁用（可用模型/任务: {models}）")
        except Exception as e:
            self._use_vision_check = False
            self.ctx.logger.warning(f"检测宿主 vlm 任务失败，识图验证已禁用: {e}")

    async def _vision_llm(self, img_b64: str, prompt: str) -> str:
        """调用宿主 vlm 任务识别截图文字，供 recorder.vision_check 使用"""
        result = await self.ctx.llm.generate(
            prompt=[{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
            model="vlm",
        )
        if not result.get("success"):
            raise RuntimeError(f"LLM 调用失败: {result.get('response') or result}")
        return str(result.get("response") or "")

    # ==================== 系统依赖 ====================

    def _ensure_python_deps(self):
        """检查并自动安装插件 requirements.txt 中的 Python 依赖"""
        if importlib.util.find_spec("camoufox") is not None:
            return

        requirements_path = Path(__file__).with_name("requirements.txt")
        if not requirements_path.exists():
            logger.error(f"未找到依赖文件: {requirements_path}")
            return

        logger.info("检测到缺少 Python 依赖 camoufox，正在自动安装 requirements.txt...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(requirements_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )
            importlib.invalidate_caches()
            logger.info("Python 依赖安装完成")
        except Exception as e:
            logger.error(
                f"自动安装 Python 依赖失败: {e}。请手动执行: "
                f"{sys.executable} -m pip install -r {requirements_path}"
            )

    @staticmethod
    def _parse_timezone(tz_str: str) -> ZoneInfo:
        """解析时区字符串，无效时回退到 Asia/Shanghai"""
        try:
            return ZoneInfo(tz_str)
        except Exception as e:
            logger.warning(f"无效的时区 '{tz_str}'，使用默认时区 Asia/Shanghai: {e}")
            return ZoneInfo("Asia/Shanghai")

    def _ensure_system_deps(self):
        """在 Linux 上检查并自动安装 Camoufox 所需的系统库"""
        import platform
        if platform.system() != "Linux":
            return

        import ctypes
        required_libs = [
            "libgtk-3.so.0",
            "libdbus-glib-1.so.2",
            "libasound.so.2",
            "libXcomposite.so.1",
            "libXdamage.so.1",
            "libXrandr.so.2",
            "libgbm.so.1",
            "libpango-1.0.so.0",
            "libatk-1.0.so.0",
            "libatk-bridge-2.0.so.0",
            "libcups.so.2",
        ]
        missing = []
        for lib in required_libs:
            try:
                ctypes.cdll.LoadLibrary(lib)
            except OSError:
                missing.append(lib)

        if not missing:
            return

        logger.info(f"检测到缺少 {len(missing)} 个系统库，正在自动安装...")

        # 检测发行版并选择包管理器
        distro_id = self._detect_distro()
        pm_info = self._get_package_manager(distro_id)

        if not pm_info:
            logger.warning(
                f"未识别的 Linux 发行版 ({distro_id})，请手动安装以下库:\n"
                f"  {', '.join(missing)}")
            return

        pm_name = pm_info["name"]
        packages = pm_info["packages"]
        install_cmd = pm_info["install_cmd"]
        update_cmd = pm_info.get("update_cmd")

        logger.info(f"检测到包管理器: {pm_name}")

        import shutil

        if not shutil.which(pm_name):
            logger.warning(
                f"未找到包管理器 {pm_name}，请手动安装以下库:\n"
                f"  {', '.join(missing)}")
            return

        try:
            if update_cmd:
                subprocess.run(
                    update_cmd, check=True, capture_output=True, timeout=120,
                )
            subprocess.run(
                install_cmd + packages,
                check=True, capture_output=True, timeout=300,
            )
            logger.info("系统依赖安装完成")
        except subprocess.CalledProcessError:
            manual_cmd = " ".join(install_cmd + packages)
            logger.warning(
                f"自动安装系统依赖失败（可能需要 root 权限），请手动执行:\n"
                f"  {manual_cmd}")
        except Exception as e:
            logger.warning(f"检查系统依赖时出错: {e}")

    @staticmethod
    def _detect_distro() -> str:
        """通过 /etc/os-release 检测 Linux 发行版 ID"""
        try:
            with open("/etc/os-release", "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ID="):
                        return line.split("=", 1)[1].strip('"').lower()
                    if line.startswith("ID_LIKE="):
                        return line.split("=", 1)[1].strip('"').lower()
        except FileNotFoundError:
            pass
        return ""

    @staticmethod
    def _get_package_manager(distro_id: str) -> dict | None:
        """根据发行版 ID 返回包管理器信息"""
        # Debian / Ubuntu 系
        apt_packages = [
            "libgtk-3-0", "libdbus-glib-1-2", "libasound2",
            "libx11-xcb1", "libxcomposite1", "libxdamage1", "libxrandr2",
            "libgbm1", "libpango-1.0-0", "libatk1.0-0", "libatk-bridge2.0-0",
            "libcups2", "libxkbcommon0", "libatspi2.0-0",
        ]
        # RHEL / CentOS / Fedora 系
        rpm_packages = [
            "gtk3", "dbus-glib", "alsa-lib",
            "libxcb", "libXcomposite", "libXdamage", "libXrandr",
            "mesa-libgbm", "pango", "atk", "at-spi2-atk",
            "cups-libs", "libxkbcommon", "at-spi2-core",
        ]
        # Arch Linux 系
        pacman_packages = [
            "gtk3", "dbus-glib", "alsa-lib",
            "libxcomposite", "libxdamage", "libxrandr",
            "mesa", "pango", "atk", "at-spi2-atk",
            "libcups", "libxkbcommon", "at-spi2-core",
        ]
        # openSUSE 系
        zypper_packages = [
            "gtk3", "dbus-1-glib", "alsa-lib",
            "libX11-xcb1", "libXcomposite1", "libXdamage1", "libXrandr2",
            "libgbm1", "pango", "atk", "at-spi2-atk",
            "libcups2", "libxkbcommon0", "at-spi2-core",
        ]

        # 发行版 → 包管理器映射
        apt_distros = {"debian", "ubuntu", "linuxmint", "pop", "elementary",
                       "zorin", "kali", "raspbian", "deepin", "uos"}
        dnf_distros = {"fedora"}
        yum_distros = {"rhel", "centos", "amzn", "ol", "rocky", "almalinux",
                       "cloudlinux", "eurolinux", "scientific"}
        pacman_distros = {"arch", "manjaro", "endeavouros", "garuda", "artix"}
        zypper_distros = {"opensuse", "sles", "suse"}

        # 支持 ID_LIKE 包含多个值的情况（如 "rhel fedora"）
        ids = set(distro_id.split())

        if ids & apt_distros or "debian" in distro_id or "ubuntu" in distro_id:
            return {
                "name": "apt-get",
                "packages": apt_packages,
                "update_cmd": ["apt-get", "update", "-qq"],
                "install_cmd": ["apt-get", "install", "-y", "-qq"],
            }
        elif ids & dnf_distros:
            return {
                "name": "dnf",
                "packages": rpm_packages,
                "install_cmd": ["dnf", "install", "-y"],
            }
        elif ids & yum_distros or "rhel" in distro_id:
            # 优先尝试 dnf（RHEL 8+ 默认），回退 yum
            import shutil
            pm = "dnf" if shutil.which("dnf") else "yum"
            return {
                "name": pm,
                "packages": rpm_packages,
                "install_cmd": [pm, "install", "-y"],
            }
        elif ids & pacman_distros or "arch" in distro_id:
            return {
                "name": "pacman",
                "packages": pacman_packages,
                "install_cmd": ["pacman", "-S", "--noconfirm", "--needed"],
                "update_cmd": ["pacman", "-Sy"],
            }
        elif ids & zypper_distros or "suse" in distro_id:
            return {
                "name": "zypper",
                "packages": zypper_packages,
                "install_cmd": ["zypper", "install", "-y"],
                "update_cmd": ["zypper", "refresh"],
            }

        return None

    # ==================== Camoufox 初始化 ====================

    async def _ensure_camoufox_binary(self):
        """检查并自动下载 Camoufox 浏览器二进制"""
        try:
            from camoufox.pkgman import launch_path
            exe_path = launch_path()
            if os.path.exists(exe_path):
                logger.info("Camoufox 浏览器二进制已就绪")
                return
        except Exception:
            pass

        logger.info("正在自动下载 Camoufox 浏览器二进制，首次运行需要等待...")
        try:
            from camoufox.pkgman import CamoufoxFetcher
            fetcher = CamoufoxFetcher()
            fetcher.fetch_latest()
            fetcher.install()
            logger.info("Camoufox 浏览器二进制下载完成")
        except Exception as e:
            logger.error(f"自动下载 Camoufox 浏览器失败: {e}，请手动执行: python -m camoufox fetch")

    # ==================== 定时调度 ====================

    async def _scheduler_loop(self):
        """定时签到调度循环 — 每分钟检查 cron 规则是否匹配"""
        if not self.cron_rules:
            logger.warning("未配置有效的 Cron 规则，定时签到已禁用")
            return

        last_fire_minute = ""  # 防止同一分钟内重复触发

        while True:
            try:
                await asyncio.sleep(30)  # 每 30 秒检查一次
                now = datetime.now(self.timezone)
                minute_key = now.strftime("%Y%m%d%H%M")

                if minute_key == last_fire_minute:
                    continue

                for rule in self.cron_rules:
                    if rule.matches(now):
                        last_fire_minute = minute_key
                        logger.info(f"Cron 规则 [{rule.expr}] 触发签到")
                        await self._do_scheduled_checkin()
                        break  # 同一分钟只触发一次

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时签到调度异常: {e}")
                await asyncio.sleep(60)

    async def _idle_check_loop(self):
        """定期检查浏览器空闲时间，超时则自动关闭"""
        timeout_seconds = self.config.browser.idle_timeout * 60
        while True:
            try:
                await asyncio.sleep(30)  # 每30秒检查一次
                if self.browser_manager and self.browser_manager.is_running:
                    idle = self.browser_manager.idle_seconds
                    if idle >= timeout_seconds:
                        logger.info(
                            f"浏览器已空闲 {idle/60:.1f} 分钟，自动关闭"
                        )
                        await self.browser_manager.shutdown()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"空闲检查异常: {e}")

    async def _do_scheduled_checkin(self):
        """执行定时签到并发送通知"""
        logger.info("开始执行定时签到...")

        results = await run_all_checkins(
            self.browser_manager, self.checkin_manager,
            self.config.browser.action_delay,
            vision_llm=self._vision_llm,
            use_vision_check=self._use_vision_check,
            checkin_wait=self.config.browser.checkin_wait,
        )

        # 构建通知消息
        msg = self._format_checkin_result(results)
        logger.info(f"定时签到完成: {msg}")

        # 发送通知给所有绑定的会话
        await self._send_notifications(msg)

    def _format_checkin_result(self, results: dict) -> str:
        """格式化签到结果为消息文本"""
        success_list = results.get("success", [])
        failed_list = results.get("failed", [])
        message = results.get("message", "")

        if message:
            return f"[自动签到] {message}"

        lines = ["[自动签到] 每日签到执行完毕"]
        lines.append(f"成功: {len(success_list)} | 失败: {len(failed_list)}")

        if success_list:
            lines.append(f"\n已签到: {', '.join(success_list)}")

        if failed_list:
            lines.append("\n未成功列表:")
            for f in failed_list:
                lines.append(f"  - {f['name']}: {f['error']}")

        if not failed_list:
            lines.append("\n全部签到完成!")

        return "\n".join(lines)

    # ==================== 通知管理 ====================

    def _load_notify_targets(self):
        """加载通知目标（stream_id 列表）"""
        target_file = Path(self.data_dir) / "notify_targets.json"
        if target_file.exists():
            try:
                with open(target_file, "r", encoding="utf-8") as f:
                    self._notify_targets = json.load(f)
            except Exception:
                self._notify_targets = []

    def _save_notify_targets(self):
        """保存通知目标"""
        target_file = Path(self.data_dir) / "notify_targets.json"
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(self._notify_targets, f)

    async def _send_notifications(self, msg: str):
        """向所有绑定的会话发送通知"""
        for stream_id in self._notify_targets:
            try:
                await self.ctx.send.text(msg, stream_id)
            except Exception as e:
                logger.warning(f"发送通知到 {stream_id} 失败: {e}")

    # ==================== 命令 ====================

    @Command(
        "checkin",
        description="自动签到管理：/签到 执行|状态|绑定|解绑|面板",
        pattern=r"^/签到(?:\s+(?P<sub>执行|状态|绑定|解绑|面板))?\s*$",
    )
    async def cmd_checkin(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = kwargs.get("stream_id", "")
        sub = (kwargs.get("matched_groups") or {}).get("sub") or ""

        if not self._services_started:
            await self.ctx.send.text("自动签到插件服务未就绪（可能正在启动或启动失败），请检查 MaiBot 日志。", stream_id)
            return False, "插件服务未就绪", 2

        if sub == "执行":
            return await self._cmd_run_all(stream_id)
        if sub == "状态":
            return await self._cmd_status(stream_id)
        if sub == "绑定":
            return await self._cmd_bind(stream_id)
        if sub == "解绑":
            return await self._cmd_unbind(stream_id)
        if sub == "面板":
            return await self._cmd_panel(stream_id)

        help_text = (
            "[自动签到] 可用命令:\n"
            "/签到 执行 - 立即执行全部签到\n"
            "/签到 状态 - 查看站点列表与定时计划\n"
            "/签到 单签 <站点名> - 签到指定站点\n"
            "/签到 绑定 - 绑定当前会话接收签到通知\n"
            "/签到 解绑 - 解除签到通知绑定\n"
            "/签到 面板 - 获取 WebUI 控制面板地址"
        )
        await self.ctx.send.text(help_text, stream_id)
        return True, "已发送帮助", 2

    async def _cmd_run_all(self, stream_id: str) -> tuple[bool, str, int]:
        """立即执行全部签到"""
        sites = self.checkin_manager.get_enabled_sites()
        if not sites:
            await self.ctx.send.text(
                "没有已启用的站点。请先在 WebUI 中添加站点并录制签到操作。", stream_id)
            return False, "没有已启用的站点", 2

        await self.ctx.send.text(f"开始签到 {len(sites)} 个站点，请稍候...", stream_id)

        results = await run_all_checkins(
            self.browser_manager, self.checkin_manager,
            self.config.browser.action_delay,
            vision_llm=self._vision_llm,
            use_vision_check=self._use_vision_check,
            checkin_wait=self.config.browser.checkin_wait,
        )
        msg = self._format_checkin_result(results)
        await self.ctx.send.text(msg, stream_id)
        return True, "签到执行完毕", 2

    async def _cmd_status(self, stream_id: str) -> tuple[bool, str, int]:
        """查看签到状态和站点列表"""
        sites = self.checkin_manager.get_all_sites()
        if not sites:
            await self.ctx.send.text(
                "暂无站点配置。\n"
                f"请访问 WebUI 添加站点: http://127.0.0.1:{self.config.webui.port}",
                stream_id,
            )
            return True, "暂无站点配置", 2

        lines = [
            f"[自动签到] 共 {len(sites)} 个站点",
            f"定时计划: {format_cron_for_display(self.cron_rules)}",
            "",
        ]
        for f in sites:
            if f["last_checkin"]:
                lines.append(f"  {f['name']} | {f['last_result']} | {f['last_checkin']}")
            else:
                lines.append(f"  {f['name']} | 从未签到")

        await self.ctx.send.text("\n".join(lines), stream_id)
        return True, "已发送签到状态", 2

    async def _cmd_bind(self, stream_id: str) -> tuple[bool, str, int]:
        """绑定当前会话接收签到结果通知"""
        if not stream_id:
            return False, "无法获取当前会话标识", 2
        if stream_id in self._notify_targets:
            await self.ctx.send.text("当前会话已绑定签到通知。", stream_id)
            return True, "已绑定", 2

        self._notify_targets.append(stream_id)
        self._save_notify_targets()
        await self.ctx.send.text(
            "已绑定! 每日签到完成后将在此会话推送结果。\n"
            f"计划: {format_cron_for_display(self.cron_rules)}",
            stream_id,
        )
        return True, "绑定成功", 2

    async def _cmd_unbind(self, stream_id: str) -> tuple[bool, str, int]:
        """解除当前会话的签到通知绑定"""
        if stream_id not in self._notify_targets:
            await self.ctx.send.text("当前会话未绑定签到通知。", stream_id)
            return True, "未绑定", 2

        self._notify_targets.remove(stream_id)
        self._save_notify_targets()
        await self.ctx.send.text("已解绑签到通知。", stream_id)
        return True, "解绑成功", 2

    async def _cmd_panel(self, stream_id: str) -> tuple[bool, str, int]:
        """获取 WebUI 控制面板地址"""
        await self.ctx.send.text(
            f"[自动签到] WebUI 控制面板\n"
            f"地址: http://127.0.0.1:{self.config.webui.port}\n\n"
            f"功能说明:\n"
            f"1. 启动浏览器后，在画面中操作登录站点\n"
            f"2. 添加站点并录制签到点击操作\n"
            f"3. 保存录制后即可自动定时签到",
            stream_id,
        )
        return True, "已发送面板地址", 2

    @Command(
        "checkin_one",
        description="签到指定站点：/签到 单签 <站点名>",
        pattern=r"^/签到\s+单签\s+(?P<site>\S+)\s*$",
    )
    async def cmd_checkin_one(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = kwargs.get("stream_id", "")
        site_name = ((kwargs.get("matched_groups") or {}).get("site") or "").strip()

        if not self._services_started:
            await self.ctx.send.text("自动签到插件服务未就绪（可能正在启动或启动失败），请检查 MaiBot 日志。", stream_id)
            return False, "插件服务未就绪", 2

        site = self.checkin_manager.get_site(site_name)
        if not site:
            await self.ctx.send.text(f"未找到站点: {site_name}", stream_id)
            return False, "站点不存在", 2

        if not site.actions:
            await self.ctx.send.text(
                f"{site_name} 尚未录制签到操作，请先在 WebUI 中录制。", stream_id)
            return False, "站点未录制操作", 2

        await self.ctx.send.text(f"正在签到: {site_name}...", stream_id)

        if not self.browser_manager.is_running:
            try:
                await self.browser_manager.launch()
            except Exception as e:
                await self.ctx.send.text(f"浏览器启动失败: {e}", stream_id)
                return False, "浏览器启动失败", 2

        outcome = await execute_site_checkin(
            self.browser_manager, self.checkin_manager, site,
            self.config.browser.action_delay,
            vision_llm=self._vision_llm,
            use_vision_check=self._use_vision_check,
            checkin_wait=self.config.browser.checkin_wait,
        )

        if outcome["success"]:
            if outcome["raw_result"] == "already_checked_in":
                await self.ctx.send.text(f"{site_name} 今日已签到，已跳过。", stream_id)
            elif outcome["result"].startswith("识图验证: "):
                await self.ctx.send.text(
                    f"{site_name} 签到成功! {outcome['result']}", stream_id)
            else:
                await self.ctx.send.text(f"{site_name} 签到成功!", stream_id)
            return True, f"{site_name} 签到成功", 2

        await self.ctx.send.text(f"{site_name} 签到失败: {outcome['result']}", stream_id)
        return False, f"{site_name} 签到失败", 2


def create_plugin() -> AutoCheckinPlugin:
    """创建插件实例"""
    return AutoCheckinPlugin()
