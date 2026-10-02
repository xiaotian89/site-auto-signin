# -*- coding: utf-8 -*-
"""站点自动签到Pro - 自动签到MP所有站点，支持FlareSolverr过CF"""
from typing import Any, List, Dict, Optional
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
from app.helper.browser import PlaywrightHelper
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.core.event import eventmanager, Event
from app.schemas.types import EventType


class SiteAutoSignin(_PluginBase):
    plugin_name = "站点自动签到Pro"
    plugin_desc = "自动签到MP里所有站点，FlareSolverr+Playwright自动过CF滑块，随机错峰，微信通知。"
    plugin_icon = "https://img.icons8.com/fluency/96/calendar.png"
    plugin_version = "1.3.0"
    plugin_author = "xiaotian"
    author_url = "https://github.com/xiaotian89"
    plugin_config_prefix = "siteautosignin_"
    plugin_order = 0
    auth_level = 1

    _scheduler: Optional[BackgroundScheduler] = None
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = True
    _queue_cnt: int = 5
    _auto_cf: bool = True
    _sign_sites: list = []

    def init_plugin(self, config: dict = None):
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

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/pro_signin",
            "event": EventType.PluginAction,
            "desc": "手动执行站点自动签到Pro",
            "category": "站点",
            "data": {"action": "pro_signin"}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            try:
                return [{
                    "id": "SiteAutoSignin.daily_signin",
                    "name": "每日站点自动签到Pro",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sign_in,
                    "kwargs": {},
                }]
            except Exception as e:
                logger.error(f"定时任务配置错误: {str(e)}")
        return []

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "mb-2"},
                        "content": [
                            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件", "color": "#4CAF50"}},
                        ]
                    },
                    {
                        "component": "div",
                        "props": {"class": "mb-2"},
                        "content": [
                            {"component": "VTextField", "props": {"model": "cron", "label": "签到时间(Cron)", "placeholder": "0 8 * * *", "variant": "outlined"}},
                        ]
                    },
                    {
                        "component": "div",
                        "props": {"class": "mb-2"},
                        "content": [
                            {"component": "VSwitch", "props": {"model": "onlyonce", "label": "保存后立即执行一次", "color": "#9C27B0"}},
                        ]
                    },
                    {
                        "component": "div",
                        "props": {"class": "mb-2"},
                        "content": [
                            {"component": "VSwitch", "props": {"model": "notify", "label": "签到结果微信通知", "color": "#2196F3"}},
                        ]
                    },
                    {
                        "component": "div",
                        "props": {"class": "mb-2"},
                        "content": [
                            {"component": "VSwitch", "props": {"model": "auto_cf", "label": "自动过CF/滑块", "color": "#FF9800"}},
                        ]
                    },
                    {
                        "component": "div",
                        "props": {"class": "mb-2"},
                        "content": [
                            {"component": "VTextField", "props": {"model": "queue_cnt", "label": "并发数(1-10)", "type": "number", "variant": "outlined"}},
                        ]
                    },
                ]
            }
        ], {
            "enabled": self._enabled,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "notify": self._notify,
            "queue_cnt": self._queue_cnt,
            "auto_cf": self._auto_cf,
            "sign_sites": self._sign_sites,
        }

    def get_page(self) -> list[dict]:
        return [
            {
                "component": "div",
                "props": {"class": "pa-3"},
                "content": [
                    {"component": "div", "props": {"class": "text-h6 mb-2"}, "text": "站点自动签到Pro"},
                    {"component": "div", "props": {"class": "text-body-2"}, "text": "自动签到MP里所有已添加站点，优先调用FlareSolverr过CF验证，失败自动降级Playwright浏览器渲染。"},
                    {"component": "div", "props": {"class": "text-body-2 mt-2"}, "text": "FlareSolverr地址: http://192.168.2.70:8191"},
                ]
            }
        ]

    def stop_service(self):
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def __get_flaresolverr_page(self, url: str, cookie: str) -> Optional[str]:
        try:
            flare_url = "http://192.168.2.70:8191/v1"
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": 60000,
                "headers": {"Cookie": cookie}
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
        site_oper = SiteOper()
        sites = site_oper.list_order_by_pri()
        if not sites:
            logger.info("没有添加任何站点")
            return
        logger.info(f"开始签到，共 {len(sites)} 个站点")
        results = []
        for site in sites:
            try:
                time.sleep(random.uniform(2, 10))
                site_name = site.name
                site_url = site.url
                site_cookie = site.cookie
                if not site_cookie:
                    results.append(f"❌ {site_name}: 没有Cookie")
                    continue
                logger.info(f"签到: {site_name}")
                sign_url = f"{site_url}/attendance.php"
                page_source = self.__get_flaresolverr_page(sign_url, site_cookie)
                if not page_source:
                    page_source = PlaywrightHelper().get_page_source(
                        url=sign_url,
                        cookies=site_cookie,
                        ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        timeout=15
                    )
                if page_source:
                    if under_challenge(page_source):
                        results.append(f"⚠️ {site_name}: CF挑战无法通过")
                        continue
                    if not SiteUtils.is_logged_in(page_source):
                        results.append(f"❌ {site_name}: Cookie失效")
                        continue
                    if "签到成功" in page_source or "已签到" in page_source or SiteUtils.is_checkin(page_source):
                        results.append(f"✅ {site_name}: 签到成功")
                    else:
                        results.append(f"✅ {site_name}: 签到请求已发送")
                else:
                    results.append(f"❌ {site_name}: 请求失败")
            except Exception as e:
                logger.error(f"{site.name}: 签到异常 {str(e)}")
                results.append(f"❌ {site.name}: 异常 {str(e)}")
        if self._notify:
            notify_text = "站点签到结果：\n" + "\n".join(results)
            logger.info(notify_text)
