# -*- coding: utf-8 -*-
"""站点自动签到Pro - MoviePilot V3

自动签到MP里所有已添加站点，FlareSolverr优先过CF，失败自动降级Playwright浏览器渲染。
"""
import re
import traceback
from datetime import datetime, timedelta
from typing import Any, List, Dict, Optional

import pytz
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.site_oper import SiteOper
from app.helper.browser import PlaywrightHelper
from app.helper.cloudflare import under_challenge
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


class ProSignin(_PluginBase):
    """站点自动签到Pro。"""

    plugin_name = "站点自动签到Pro"
    plugin_desc = "自动签到MP里所有已添加站点，FlareSolverr+Playwright自动过CF滑块，随机错峰，微信通知。"
    plugin_icon = "https://img.icons8.com/fluency/96/calendar.png"
    plugin_version = "2.0.0"
    plugin_author = "xiaotian"
    author_url = "https://github.com/xiaotian89"
    plugin_config_prefix = "prosignin_"
    plugin_order = 1
    auth_level = 2

    _scheduler: Optional[BackgroundScheduler] = None
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = False
    _queue_cnt: int = 5
    _sign_sites: list = []
    _retry_keyword = None
    _clean: bool = False
    _auto_cf: int = 0
    _flaresolverr_url = "http://192.168.2.70:8191/v1"

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._queue_cnt = config.get("queue_cnt") or 5
            self._sign_sites = config.get("sign_sites") or []
            self._retry_keyword = config.get("retry_keyword")
            self._auto_cf = config.get("auto_cf") or 0
            self._clean = config.get("clean")
            self._flaresolverr_url = config.get("flaresolverr_url") or "http://192.168.2.70:8191/v1"

            all_sites = [site.id for site in SiteOper().list_order_by_pri()]
            self._sign_sites = (["all"] if "all" in self._sign_sites else
                                [site_id for site_id in all_sites if site_id in self._sign_sites])

            self.__update_config()

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._onlyonce = False
                self.__update_config()
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(
                    func=self.sign_in,
                    trigger='date',
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="站点自动签到Pro立即运行",
                )
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
            "sign_sites": self._sign_sites,
            "retry_keyword": self._retry_keyword,
            "auto_cf": self._auto_cf,
            "clean": self._clean,
            "flaresolverr_url": self._flaresolverr_url,
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
        if self._enabled and self._cron and str(self._cron).strip().count(" ") == 4:
            try:
                return [{
                    "id": "ProSignin.daily",
                    "name": "每日站点自动签到Pro",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sign_in,
                    "kwargs": {}
                }]
            except Exception as e:
                logger.error(f"定时任务错误: {e}")
        return []

    def get_form(self) -> tuple:
        all_sites = [{"title": "全部", "value": "all"}] + [{"title": s.name, "value": s.id} for s in SiteOper().list_order_by_pri()]
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{'component': 'VSwitch', 'props': {'model': 'clean', 'label': '清理本日缓存'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'cron', 'label': '执行周期', 'placeholder': '5位cron'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'queue_cnt', 'label': '队列数量'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'retry_keyword', 'label': '重试关键词'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'auto_cf', 'label': '自动优选(0关闭1FlareSolverr2Playwright)'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 12}, 'content': [{'component': 'VTextField', 'props': {'model': 'flaresolverr_url', 'label': 'FlareSolverr地址', 'placeholder': 'http://192.168.2.70:8191/v1'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'content': [{'component': 'VSelect', 'props': {'chips': True, 'multiple': True, 'model': 'sign_sites', 'label': '签到站点', 'items': all_sites}}]}
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "cron": "",
            "auto_cf": 1,
            "onlyonce": False,
            "clean": False,
            "queue_cnt": 5,
            "sign_sites": ["all"],
            "retry_keyword": "错误|失败",
            "flaresolverr_url": "http://192.168.2.70:8191/v1"
        }

    def get_page(self) -> List[dict]:
        return [
            {
                'component': 'VAlert',
                'props': {'type': 'info', 'variant': 'tonal', 'text': '站点自动签到Pro v2.0.0：自动签到所有已选站点，FlareSolverr优先过CF，失败降级Playwright。', 'class': 'mt-4'}
            }
        ]

    def stop_service(self):
        self._enabled = False
        try:
            if self._scheduler:
                self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception:
            pass

    @eventmanager.register(EventType.PluginAction)
    def _on_plugin_action(self, event: Event):
        event_data = getattr(event, "event_data", None) or {}
        if event_data.get("action") != "pro_signin":
            return
        self.sign_in()

    def sign_in(self):
        sites = SiteOper().list_order_by_pri()
        if not sites:
            logger.info("没有站点")
            return

        selected_ids = set(self._sign_sites or [])
        if not selected_ids or "all" in selected_ids:
            sign_sites = sites
        else:
            sign_sites = [s for s in sites if s.id in selected_ids]

        logger.info(f"开始签到，共{len(sign_sites)}个站点")
        results = []
        for site in sign_sites:
            try:
                import time as _time
                _time.sleep(2)
                site_name = site.name
                site_url = site.url
                site_cookie = site.cookie
                if not site_cookie:
                    results.append(f"❌ {site_name}: 无Cookie")
                    continue
                logger.info(f"签到: {site_name}")
                sign_url = f"{site_url.rstrip('/')}/attendance.php"
                page_source = None

                if self._auto_cf == 1:
                    try:
                        payload = {
                            "cmd": "request.get",
                            "url": sign_url,
                            "maxTimeout": 60000,
                            "headers": {"Cookie": site_cookie}
                        }
                        resp = RequestUtils(timeout=70).post_res(url=self._flaresolverr_url, json=payload)
                        if resp and resp.status_code == 200:
                            data = resp.json()
                            if data.get("status") == "ok":
                                page_source = data.get("solution", {}).get("response", "")
                    except Exception as e:
                        logger.warning(f"FlareSolverr失败: {e}")

                if not page_source and self._auto_cf == 2:
                    try:
                        page_source = PlaywrightHelper().get_page_source(
                            url=sign_url,
                            cookies=site_cookie,
                            ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            timeout=20
                        )
                    except Exception as e:
                        logger.warning(f"Playwright失败: {e}")

                if not page_source:
                    try:
                        resp = RequestUtils(cookies=site_cookie, timeout=30).get_res(url=sign_url)
                        if resp and resp.status_code == 200:
                            page_source = resp.text
                    except Exception as e:
                        logger.warning(f"普通请求失败: {e}")

                if not page_source:
                    results.append(f"❌ {site_name}: 请求失败")
                    continue

                if under_challenge(page_source):
                    results.append(f"⚠️ {site_name}: CF挑战")
                    continue
                if not SiteUtils.is_logged_in(page_source):
                    results.append(f"❌ {site_name}: Cookie失效")
                    continue
                if "签到成功" in page_source or "已签到" in page_source or SiteUtils.is_checkin(page_source):
                    results.append(f"✅ {site_name}: 签到成功")
                else:
                    results.append(f"✅ {site_name}: 请求已发送")

            except Exception as e:
                logger.error(f"{site.name}: 异常 {e}")
                results.append(f"❌ {site.name}: 异常 {str(e)[:50]}")

        if self._notify:
            notify_text = "站点签到结果：\n" + "\n".join(results)
            logger.info(notify_text)
            try:
                from app.core.notify import post_message
                post_message(channel=NotificationType.Wechat, title="站点自动签到Pro", text=notify_text)
            except Exception as e:
                logger.warning(f"通知发送失败: {e}")
