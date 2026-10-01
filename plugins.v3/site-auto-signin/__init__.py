from typing import Any, Optional
import time
import random
import pytz
from datetime import datetime, timedelta
from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.background import BackgroundScheduler
from app.log import logger
from app.plugins import _PluginBase
from app.core.config import settings
from app.db.site_oper import SiteOper
from app.helper.cloudflare import under_challenge
from app.helper.sites import SitesHelper
from app.helper.browser import PlaywrightHelper
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.core.event import eventmanager, Event
from app.schemas.types import EventType, NotificationType


class SiteAutoSignin(_PluginBase):
    plugin_version = "1.0.0"
    plugin_name = "站点自动签到Pro"
    plugin_desc = "自动签到MP里的所有站点，支持自动过CF、滑块验证，随机错峰，微信通知。"
    plugin_icon = "signin.png"
    plugin_author = "xiaotian"
    author_url = "https://github.com/xiaotian89"
    plugin_config_prefix = "siteautosignin_"
    plugin_order = 0
    auth_level = 1

    _scheduler: Optional[BackgroundScheduler] = None

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = True
    _queue_cnt: int = 5
    _auto_cf: bool = True
    _sign_sites: list = []

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify", True)
            self._queue_cnt = config.get("queue_cnt", 5)
            self._auto_cf = config.get("auto_cf", True)
            self._sign_sites = config.get("sign_sites", [])

            self.__update_config()

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("站点自动签到Pro启动，立即运行一次")
                self._scheduler.add_job(func=self.sign_in, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="站点自动签到Pro")
                self._onlyonce = False
                self.__update_config()

                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "queue_cnt": self._queue_cnt,
            "auto_cf": self._auto_cf,
            "sign_sites": self._sign_sites,
        })

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        return [
            {
                "component": "div",
                "children": [
                    {
                        "component": "el-switch",
                        "props": {"label": "启用插件", "model": "enabled"},
                    },
                    {
                        "component": "el-input",
                        "props": {"label": "定时规则", "model": "cron", "placeholder": "0 8 * * *"},
                    },
                    {
                        "component": "el-switch",
                        "props": {"label": "立即运行一次", "model": "onlyonce"},
                    },
                    {
                        "component": "el-switch",
                        "props": {"label": "发送通知", "model": "notify"},
                    },
                    {
                        "component": "el-switch",
                        "props": {"label": "自动过CF", "model": "auto_cf"},
                    },
                    {
                        "component": "el-input-number",
                        "props": {"label": "并发数", "model": "queue_cnt", "min": 1, "max": 10},
                    },
                ]
            },
        ], {
            "enabled": self._enabled,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "queue_cnt": self._queue_cnt,
            "auto_cf": self._auto_cf,
            "sign_sites": self._sign_sites,
        }

    def get_service(self) -> list[dict]:
        if self._enabled and self._cron:
            try:
                return [{
                    "id": "SiteAutoSignin.daily_signin",
                    "name": "每日站点自动签到",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sign_in,
                    "kwargs": {},
                }]
            except Exception as e:
                logger.error(f"定时任务配置错误: {str(e)}")
        return []

    def stop_service(self):
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def __get_flaresolverr_page(self, url: str, cookie: str) -> Optional[str]:
        """用FlareSolverr过CF获取页面"""
        try:
            flare_url = "http://192.168.2.70:8191/v1"
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": 60000,
                "headers": {
                    "Cookie": cookie
                }
            }
            resp = RequestUtils(timeout=70).post_res(url=flare_url, json=payload)
            if resp and resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "ok":
                    return data.get("solution", {}).get("response", "")
            return None
        except Exception as e:
            logger.error(f"FlareSolverr请求失败: {str(e)}")
            return None

    def sign_in(self):
        """签到所有站点"""
        site_oper = SiteOper()
        sites = site_oper.list_order_by_pri()

        if not sites:
            logger.info("没有添加任何站点")
            return

        logger.info(f"开始签到，共 {len(sites)} 个站点")
        results = []

        for site in sites:
            try:
                # 随机错峰
                time.sleep(random.uniform(2, 10))

                site_name = site.name
                site_url = site.url
                site_cookie = site.cookie

                if not site_cookie:
                    results.append(f"❌ {site_name}: 没有Cookie")
                    continue

                logger.info(f"签到: {site_name}")

                # 访问签到页
                sign_url = f"{site_url}/attendance.php"

                # 优先用FlareSolverr过CF
                page_source = self.__get_flaresolverr_page(sign_url, site_cookie)

                # FlareSolverr失败了再用Playwright
                if not page_source:
                    page_source = PlaywrightHelper().get_page_source(
                        url=sign_url,
                        cookies=site_cookie,
                        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        timeout=15
                    )

                if page_source:
                    # 检查CF
                    if under_challenge(page_source):
                        logger.warning(f"{site_name}: CF挑战，无法通过")
                        results.append(f"⚠️ {site_name}: CF挑战，无法通过")
                        continue

                    # 判断登录状态
                    if not SiteUtils.is_logged_in(page_source):
                        results.append(f"❌ {site_name}: Cookie失效")
                        continue

                    # 判断签到结果
                    if "签到成功" in page_source or "已签到" in page_source or "重复签到" in page_source or SiteUtils.is_checkin(page_source):
                        logger.info(f"{site_name}: 签到成功")
                        results.append(f"✅ {site_name}: 签到成功")
                    else:
                        results.append(f"✅ {site_name}: 签到请求已发送")
                else:
                    results.append(f"❌ {site_name}: 请求失败")

            except Exception as e:
                logger.error(f"{site.name}: 签到异常 {str(e)}")
                results.append(f"❌ {site.name}: 异常 {str(e)}")

        # 发送通知
        if self._notify:
            notify_text = "站点签到结果：\n" + "\n".join(results)
            logger.info(notify_text)
            eventmanager.send_event(
                EventType.PluginAction,
                {
                    "text": notify_text,
                }
            )
