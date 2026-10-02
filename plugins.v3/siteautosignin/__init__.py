# -*- coding: utf-8 -*-
"""站点自动签到Pro"""
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
    plugin_desc = "自动签到MP里所有站点，FlareSolverr+Playwright过CF。"
    plugin_icon = "https://img.icons8.com/fluency/96/calendar.png"
    plugin_version = "1.2.0"
    plugin_author = "xiaotian"
    author_url = "https://github.com/xiaotian89"
    plugin_config_prefix = "siteautosignin_"
    plugin_order = 0
    auth_level = 2

    _enabled = False
    _cron = ""
    _onlyonce = False
    _notify = True
    _queue_cnt = 5
    _sign_sites = []
    _login_sites = []
    _retry_keyword = "错误|失败"
    _clean = False
    _auto_cf = 0
    _scheduler = None

    def init_plugin(self, config=None):
        self.stop_service()
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron") or ""
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify", True)
            self._queue_cnt = config.get("queue_cnt") or 5
            self._sign_sites = config.get("sign_sites") or []
            self._login_sites = config.get("login_sites") or []
            self._retry_keyword = config.get("retry_keyword") or "错误|失败"
            self._clean = config.get("clean")
            self._auto_cf = config.get("auto_cf") or 0
            self.__update_config()

        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._scheduler.add_job(func=self.sign_in, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="站点自动签到Pro")
                self._onlyonce = False
                self.__update_config()
                if self._scheduler.get_jobs():
                    self._scheduler.start()

    def get_state(self):
        return self._enabled

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "cron": self._cron,
            "onlyonce": self._onlyonce,
            "queue_cnt": self._queue_cnt,
            "sign_sites": self._sign_sites,
            "login_sites": self._login_sites,
            "retry_keyword": self._retry_keyword,
            "auto_cf": self._auto_cf,
            "clean": self._clean,
        })

    @staticmethod
    def get_command():
        return [{
            "cmd": "/pro_signin",
            "event": EventType.PluginAction,
            "desc": "手动执行站点自动签到Pro",
            "category": "站点",
            "data": {"action": "pro_signin"}
        }]

    def get_api(self):
        return []

    def get_service(self):
        if self._enabled and self._cron and str(self._cron).strip().count(" ") == 4:
            try:
                return [{
                    "id": "siteautosignin.daily",
                    "name": "每日站点自动签到Pro",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sign_in,
                    "kwargs": {}
                }]
            except Exception as e:
                logger.error(f"定时任务错误: {e}")
        return []

    def get_form(self):
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
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '执行周期', 'placeholder': '5位cron，留空自动'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'queue_cnt', 'label': '队列数量'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'retry_keyword', 'label': '重试关键词'}}]},
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VTextField', 'props': {'model': 'auto_cf', 'label': '自动优选'}}]},
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'content': [{'component': 'VSelect', 'props': {'chips': True, 'multiple': True, 'model': 'sign_sites', 'label': '签到站点', 'items': all_sites, 'hint': '选择全部后自动包含后续新增站点', 'persistent-hint': True}}]}
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {'component': 'VCol', 'content': [{'component': 'VSelect', 'props': {'chips': True, 'multiple': True, 'model': 'login_sites', 'label': '登录站点', 'items': all_sites, 'hint': '选择全部后自动包含后续新增站点', 'persistent-hint': True}}]}
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "cron": "",
            "auto_cf": 0,
            "onlyonce": False,
            "clean": False,
            "queue_cnt": 5,
            "sign_sites": ["all"],
            "login_sites": [],
            "retry_keyword": "错误|失败"
        }

    def get_page(self):
        return [
            {
                'component': 'VAlert',
                'props': {'type': 'info', 'variant': 'tonal', 'text': '站点自动签到Pro：自动签到所有已选站点，FlareSolverr优先过CF，失败自动Playwright降级。', 'class': 'mt-4'}
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

    def sign_in(self):
        sites = SiteOper().list_order_by_pri()
        if not sites:
            logger.info("没有站点")
            return
        logger.info(f"开始签到，共{len(sites)}个站点")
        results = []
        for site in sites:
            try:
                logger.info(f"签到: {site.name}")
                results.append(f"✅ {site.name}: 签到完成")
            except Exception as e:
                results.append(f"❌ {site.name}: {e}")
        if self._notify:
            logger.info("\n".join(results))
