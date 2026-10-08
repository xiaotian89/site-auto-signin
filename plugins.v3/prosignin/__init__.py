# -*- coding: utf-8 -*-
"""站点自动签到Pro - MoviePilot V3

自动签到MP里所有已添加站点，FlareSolverr优先过CF，失败自动降级Playwright浏览器渲染。
"""
import os
import json
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
from app.schemas.types import EventType, NotificationChannel, NotificationType
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta as _td
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger



def _log(msg):
    """带插件前缀的日志输出"""
    logger.info(f"[ProSignin] {msg}")

def _log_warn(msg):
    logger.warning(f"[ProSignin] {msg}")

def _log_error(msg):
    logger.error(f"[ProSignin] {msg}")

class ProSignin(_PluginBase):
    """站点自动签到Pro。"""

    plugin_name = "站点自动签到Pro"
    plugin_desc = "多站点自动签到，CF智能降级+失败重试+并发队列+签到历史统计。"
    plugin_icon = "https://img.icons8.com/fluency/96/calendar.png"
    plugin_version = "3.8.2"
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
    _history_file = "/config/prosignin_history.json"

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._queue_cnt = int(config.get("queue_cnt") or 5)
            self._sign_sites = config.get("sign_sites") or []
            self._retry_keyword = config.get("retry_keyword")
            self._auto_cf = int(config.get("auto_cf") or 0)
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
                _log_error(f"定时任务错误: {e}")
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
                            {'component': 'VCol', 'props': {'cols': 12, 'md': 6}, 'content': [{'component': 'VSelect', 'props': {'model': 'cron', 'label': '执行周期', 'autocomplete': True, 'clearable': True, 'placeholder': '选择或输入cron', 'items': [
                                {'title': '每天 0:00', 'value': '0 0 * * *'},
                                {'title': '每天 6:00', 'value': '0 6 * * *'},
                                {'title': '每天 7:00', 'value': '0 7 * * *'},
                                {'title': '每天 8:00', 'value': '0 8 * * *'},
                                {'title': '每天 9:00', 'value': '0 9 * * *'},
                                {'title': '每天 10:00', 'value': '0 10 * * *'},
                                {'title': '每天 12:00', 'value': '0 12 * * *'},
                                {'title': '每天 18:00', 'value': '0 18 * * * *'},
                                {'title': '每天 22:00', 'value': '0 22 * * *'},
                                {'title': '每小时', 'value': '0 * * * *'},
                                {'title': '每30分钟', 'value': '*/30 * * * *'},
                                {'title': '每10分钟', 'value': '*/10 * * * *'},
                            ]}}]},
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
        """详细数据页面：原生div+CSS，参考autosignin实现方式"""
        history = self.__load_history()
        today = datetime.now().strftime("%Y-%m-%d")
        today_data = history.get("history", {}).get(today, {})

        # 获取当前存在的站点列表（过滤已删除的站点）
        try:
            current_sites = {s.name for s in SiteOper().list_order_by_pri()}
        except Exception:
            current_sites = set()

        # 今日统计只统计当前存在的站点
        today_data_filtered = {k: v for k, v in today_data.items() if k in current_sites} if current_sites else today_data

        total = len(today_data_filtered)
        success = sum(1 for v in today_data_filtered.values() if v.get("status") == "success")
        failed = sum(1 for v in today_data_filtered.values() if v.get("status") == "failed")
        warning = sum(1 for v in today_data_filtered.values() if v.get("status") == "warning")
        history_days = len(history.get("history", {}))

        display_dates = []
        for i in range(6, -1, -1):
            d = (datetime.now() - _td(days=i)).strftime("%Y-%m-%d")
            display_dates.append(d)

        # 只收集当前存在的站点
        all_sites = set(today_data_filtered.keys())
        for d in display_dates:
            day_sites = history.get("history", {}).get(d, {}).keys()
            if current_sites:
                all_sites.update(s for s in day_sites if s in current_sites)
            else:
                all_sites.update(day_sites)
        all_sites = sorted(all_sites)

        css = ".prosignin-page{display:flex;flex-direction:column;gap:12px}.prosignin-summary{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.prosignin-stat{min-width:0;padding:10px 12px;border:1px solid rgba(var(--v-theme-on-surface),.08);border-radius:8px}.prosignin-stat__head{display:flex;align-items:center;gap:6px;color:rgba(var(--v-theme-on-surface),.6);font-size:.75rem;font-weight:600}.prosignin-stat__value{margin-top:8px;font-size:1.25rem;font-weight:700;line-height:1}.prosignin-stat__meta{margin-top:4px;color:rgba(var(--v-theme-on-surface),.5);font-size:.72rem}.prosignin-section-title{font-size:.95rem;font-weight:700;margin-bottom:8px}.prosignin-table-wrap{overflow-x:auto;border:1px solid rgba(var(--v-theme-on-surface),.08);border-radius:8px}.prosignin-table{width:100%;border-collapse:collapse;min-width:620px}.prosignin-table th{height:34px;padding:0 8px;color:rgba(var(--v-theme-on-surface),.62);font-size:.75rem;font-weight:600;white-space:nowrap;text-align:left;border-bottom:1px solid rgba(var(--v-theme-on-surface),.08)}.prosignin-table td{height:38px;padding:0 8px;vertical-align:middle;border-bottom:1px solid rgba(var(--v-theme-on-surface),.05)}.prosignin-table tbody tr:last-child td{border-bottom:0}.prosignin-site-name{max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}.prosignin-status-cell{min-width:120px}.prosignin-dot-cell{width:40px;text-align:center}.prosignin-dot{width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center;border-radius:999px;border:1px solid transparent;font-weight:700;font-size:12px}.prosignin-dot--success{color:rgb(var(--v-theme-success));background:rgba(var(--v-theme-success),.24);border-color:rgba(var(--v-theme-success),.38)}.prosignin-dot--warning{color:rgb(var(--v-theme-warning));background:rgba(var(--v-theme-warning),.30);border-color:rgba(var(--v-theme-warning),.48)}.prosignin-dot--error{color:rgb(var(--v-theme-error));background:rgba(var(--v-theme-error),.26);border-color:rgba(var(--v-theme-error),.42)}.prosignin-dot--none{color:rgba(var(--v-theme-on-surface),.68);background:rgba(var(--v-theme-on-surface),.14);border-color:rgba(var(--v-theme-on-surface),.22)}@media(max-width:720px){.prosignin-summary{grid-template-columns:repeat(2,minmax(0,1fr))}.prosignin-table{min-width:560px}}"

        # 构建表头
        header_cells = [
            {'component': 'th', 'text': '站点'},
            {'component': 'th', 'text': '今日状态'},
        ]
        for d in display_dates:
            header_cells.append({'component': 'th', 'props': {'class': 'prosignin-dot-cell'}, 'text': d[5:]})

        # 构建表格行
        body_rows = []
        if all_sites:
            for site in all_sites:
                today_info = today_data.get(site, {"status": "none", "message": ""})
                today_status = today_info.get("status", "none")
                today_msg = today_info.get("message", "")

                if today_status == "success":
                    sc = "prosignin-dot--success"; si = "✓"; st = today_msg or "签到成功"
                elif today_status == "failed":
                    sc = "prosignin-dot--error"; si = "✗"; st = today_msg or "签到失败"
                elif today_status == "warning":
                    sc = "prosignin-dot--warning"; si = "!"; st = today_msg or "异常"
                else:
                    sc = "prosignin-dot--none"; si = "-"; st = "未记录"

                row_cells = [
                    {'component': 'td', 'content': [{'component': 'div', 'props': {'class': 'prosignin-site-name'}, 'text': site}]},
                    {'component': 'td', 'props': {'class': 'prosignin-status-cell'}, 'content': [
                        {'component': 'span', 'props': {'class': 'prosignin-dot ' + sc, 'style': 'margin-right:6px;'}, 'text': si},
                        {'component': 'span', 'text': st},
                    ]},
                ]

                for d in display_dates:
                    day_info = history.get("history", {}).get(d, {}).get(site, {})
                    ds = day_info.get("status", "none")
                    dm = day_info.get("message", "")
                    if ds == "success":
                        dc = "prosignin-dot--success"; di = "✓"
                    elif ds == "failed":
                        dc = "prosignin-dot--error"; di = "✗"
                    elif ds == "warning":
                        dc = "prosignin-dot--warning"; di = "!"
                    else:
                        dc = "prosignin-dot--none"; di = "-"
                    row_cells.append({'component': 'td', 'props': {'class': 'prosignin-dot-cell'}, 'content': [
                        {'component': 'span', 'props': {'class': 'prosignin-dot ' + dc}, 'text': di},
                    ]})

                body_rows.append({'component': 'tr', 'content': row_cells})
        else:
            body_rows.append({'component': 'tr', 'content': [
                {'component': 'td', 'props': {'colspan': '9', 'style': 'text-align:center;padding:24px;color:rgba(var(--v-theme-on-surface),.56);'}, 'text': '暂无签到记录，请先运行一次签到'},
            ]})

        page = [
            {'component': 'style', 'text': css},
            {'component': 'div', 'props': {'class': 'prosignin-page'}, 'content': [
                {'component': 'div', 'props': {'class': 'prosignin-summary'}, 'content': [
                    {'component': 'div', 'props': {'class': 'prosignin-stat'}, 'content': [
                        {'component': 'div', 'props': {'class': 'prosignin-stat__head'}, 'text': '📊 今日签到'},
                        {'component': 'div', 'props': {'class': 'prosignin-stat__value'}, 'text': str(success) + '/' + str(total)},
                        {'component': 'div', 'props': {'class': 'prosignin-stat__meta'}, 'text': '失败' + str(failed) + ' · 异常' + str(warning)},
                    ]},
                    {'component': 'div', 'props': {'class': 'prosignin-stat'}, 'content': [
                        {'component': 'div', 'props': {'class': 'prosignin-stat__head'}, 'text': '✅ 签到成功'},
                        {'component': 'div', 'props': {'class': 'prosignin-stat__value'}, 'text': str(success)},
                    ]},
                    {'component': 'div', 'props': {'class': 'prosignin-stat'}, 'content': [
                        {'component': 'div', 'props': {'class': 'prosignin-stat__head'}, 'text': '❌ 签到失败'},
                        {'component': 'div', 'props': {'class': 'prosignin-stat__value'}, 'text': str(failed)},
                    ]},
                    {'component': 'div', 'props': {'class': 'prosignin-stat'}, 'content': [
                        {'component': 'div', 'props': {'class': 'prosignin-stat__head'}, 'text': '📅 历史记录'},
                        {'component': 'div', 'props': {'class': 'prosignin-stat__value'}, 'text': str(history_days) + '天'},
                        {'component': 'div', 'props': {'class': 'prosignin-stat__meta'}, 'text': '最近7天详情见下表'},
                    ]},
                ]},
                {'component': 'div', 'props': {}, 'content': [
                    {'component': 'div', 'props': {'class': 'prosignin-section-title'}, 'text': '签到状态（最近7天）'},
                    {'component': 'div', 'props': {'class': 'prosignin-table-wrap'}, 'content': [
                        {'component': 'table', 'props': {'class': 'prosignin-table'}, 'content': [
                            {'component': 'thead', 'content': [{'component': 'tr', 'content': header_cells}]},
                            {'component': 'tbody', 'content': body_rows},
                        ]}
                    ]},
                ]},
                {'component': 'div', 'props': {'style': 'color:rgba(var(--v-theme-on-surface),.5);font-size:.72rem;margin-top:4px;'}, 'text': '站点自动签到Pro v3.5.8 · 并发队列+失败重试+智能降级 · 历史记录保存在 /config/prosignin_history.json，保留最近30天 · 已删除站点自动过滤'},
            ]}
        ]
        return page

    # 各站点签到URL硬编码映射（避免每次试探，减少Cookie过期风险）
    _SIGN_URL_MAP = {
        # 特殊签到URL
        '52pt': '52bakatestdate0823.php',
        'pttime': 'attendance.php?type=list',
        # 需要点击签到按钮的站点（Playwright自动点击）
        'btschool': 'index.php?action=sign',
        'hdarea': 'index.php',
        'ourbits': 'attendance.php',
        'audiences': 'attendance.php',
        # 以下站点默认都是 attendance.php，显式列出便于维护
        'hdsky': 'attendance.php',
        'ptchdbits': 'attendance.php',
        'open.cd': 'attendance.php',
        'ubits': 'attendance.php',
        'keepfrds': 'attendance.php',
        'hdhome': 'attendance.php',
        'discfan': 'attendance.php',
        'crabpt': 'attendance.php',
        'ptskit': 'attendance.php',
        'xingyungept': 'attendance.php',
        'agsvpt': 'attendance.php',
        'dubhe': 'attendance.php',
        'hhanclub': 'attendance.php',
        'pterclub': 'attendance.php',
        'cspt': 'attendance.php',
        'sewerpt': 'attendance.php',
        'pandapt': 'attendance.php',
        'hddolby': 'attendance.php',
        'cyanbug': 'attendance.php',
        'zmpt': 'attendance.php',
        'qingwapt': 'attendance.php',
        'carpt': 'attendance.php',
        'nicept': 'attendance.php',
        'pt.0ff': 'attendance.php',
    }

    def __sign_one_site(self, site):
        """签到单个站点，无重试（避免加速Cookie过期）"""
        import time as _time
        max_retries = 0
        retry_keywords = [kw.strip() for kw in (self._retry_keyword or "").split("|") if kw.strip()]

        for attempt in range(max_retries + 1):
            try:
                if attempt > 0:
