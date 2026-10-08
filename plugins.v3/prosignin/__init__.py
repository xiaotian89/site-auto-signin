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
                    _log(f"{site.name}: 第{attempt}次重试")
                    _time.sleep(3)

                site_name = site.name
                site_url = site.url
                # API签到站点特殊处理（馒头、肉丝、朱雀）
                site_url_lower = site_url.lower()
                site_name_lower = site_name.lower()
                if "m-team" in site_url_lower or "mteam" in site_url_lower or "馒头" in site_name or "m-team" in site_name_lower:
                    api_result = self.__signin_mteam(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                elif "rousi" in site_url_lower or "肉丝" in site_name or "rousi" in site_name_lower:
                    api_result = self.__signin_rousi(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                elif "zhuque" in site_url_lower or "朱雀" in site_name or "zhuque" in site_name_lower:
                    api_result = self.__signin_zhuque(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                elif "totheglory" in site_url_lower or "听听歌" in site_name or "ttg" in site_name_lower:
                    api_result = self.__signin_ttg(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                
                site_cookie = site.cookie
                # 读取站点的浏览器仿真(render)配置
                site_render = getattr(site, 'render', False) or False
                if not site_cookie:
                    return f"❌ {site_name}: 无Cookie"

                # 使用硬编码的签到URL映射（避免每次试探，减少Cookie过期风险）
                sign_path = 'attendance.php'  # 默认
                for key, path in self._SIGN_URL_MAP.items():
                    if key in site_url_lower:
                        sign_path = path
                        break
                sign_url = f"{site_url.rstrip('/')}/{sign_path}"
                page_source = None

                # 构建代理配置（站点开启proxy时使用容器代理）
                import os as _os
                _proxies = None
                if getattr(site, 'proxy', False):
                    _proxy_url = _os.environ.get('HTTP_PROXY') or _os.environ.get('HTTPS_PROXY') or 'http://192.168.2.70:7892'
                    _proxies = {'http': _proxy_url, 'https': _proxy_url}

                # 对于需要点击签到按钮的站点（学校、高清视界、我堡、观众），直接使用Playwright自动点击
                need_auto_click_site = (
                    ("btschool" in site_url_lower or "学校" in site_name) or
                    ("hdarea" in site_url_lower or "高清视界" in site_name) or
                    ("ourbits" in site_url_lower or "我堡" in site_name) or
                    ("audiences" in site_url_lower or "观众" in site_name)
                )
                if need_auto_click_site:
                    _log(f"{site_name}: 需要点击签到按钮，直接使用Playwright自动点击...")
                    # 学校和高清视界的签到URL是index.php?action=sign，我堡和观众是attendance.php
                    if "ourbits" in site_url_lower or "我堡" in site_name or "audiences" in site_url_lower or "观众" in site_name:
                        auto_click_url = f"{site_url.rstrip('/')}/attendance.php"
                    else:
                        auto_click_url = f"{site_url.rstrip('/')}/index.php?action=sign"
                    try:
                        def _auto_click_sign(page):
                            """自动点击签到按钮的回调函数"""
                            try:
                                page.wait_for_load_state("networkidle", timeout=10000)
                            except:
                                pass
                            
                            # 签到成功关键词（用于点击后检测结果）
                            success_keywords = [
                                "签到成功", "签到完成", "成功签到", "已签到", "今日已签到",
                                "已经签到", "请勿重复签到", "获得魔力", "魔力+", "签到奖励",
                                "打卡成功", "签到已完成", "签到已得", "查看签到记录",
                                "簽到成功", "簽到完成", "簽到獎勵", "獲得魔力", "簽到已得",
                                "查看簽到記錄", "验证通过", "爆米花"
                            ]
                            
                            def _wait_for_result(site_name, max_wait=30):
                                """点击签到后循环等待结果，最多等待max_wait秒"""
                                import time as _tw
                                _log(f"{site_name}: 等待签到结果（最长{max_wait}秒）...")
                                for i in range(max_wait // 2):
                                    _tw.sleep(2)
                                    try:
                                        current_text = page.inner_text('body')
                                        if any(kw in current_text for kw in success_keywords):
                                            _log(f"{site_name}: 检测到签到结果（文本关键词）")
                                            return True
                                        # 观众站点：检测attendance-card--done元素
                                        if "audiences" in site_url_lower or "观众" in site_name:
                                            done_card = page.query_selector('.attendance-card--done')
                                            if done_card:
                                                _log(f"{site_name}: 检测到签到结果（attendance-card--done）")
                                                return True
                                    except:
                                        pass
                                _log_warn(f"{site_name}: 等待签到结果超时")
                                return False
                            
                            if "hdarea" in site_url_lower or "高清视界" in site_name:
                                # 高清视界：签到通过JS函数sign_in('sign_in')触发，不是直接访问URL
                                # 先检查是否已签到（绿色[已签到]）
                                try:
                                    already_signed = page.evaluate("""
                                        (function() {
                                            var greenFont = document.querySelector('font[color="green"]');
                                            if (greenFont && greenFont.textContent.indexOf('[已签到]') >= 0) {
                                                return true;
                                            }
                                            return false;
                                        })();
                                    """)
                                    if already_signed:
                                        _log(f"{site_name}: 检测到已签到（绿色[已签到]）")
                                        return
                                except:
                                    pass
                                # 调用sign_in函数触发签到
                                try:
                                    page.evaluate("if (typeof sign_in === 'function') { sign_in('sign_in'); }")
                                    _log(f"{site_name}: 已调用sign_in('sign_in')函数触发签到")
                                    clicked = True
                                except Exception as e:
                                    _log_warn(f"{site_name}: 调用sign_in函数失败，尝试点击[签到]链接: {e}")
                                    # 备用：点击[签到]链接
                                    try:
                                        page.click('a:has-text("[签到]")', timeout=5000)
                                        _log(f"{site_name}: 已点击[签到]链接")
                                        clicked = True
                                    except Exception as e2:
                                        _log_warn(f"{site_name}: 点击[签到]链接失败: {e2}")
                                if clicked:
                                    _wait_for_result(site_name, max_wait=20)
                            elif "btschool" in site_url_lower or "学校" in site_name:
                                # 学校：点击"每日签到"按钮，然后等待结果
                                clicked = False
                                try:
                                    page.click('a:has-text("每日签到")', timeout=5000)
                                    _log(f"{site_name}: 已点击每日签到按钮")
                                    clicked = True
                                except:
                                    try:
                                        page.click('text=每日签到', timeout=5000)
                                        _log(f"{site_name}: 已点击每日签到按钮(text选择器)")
                                        clicked = True
                                    except Exception as e2:
                                        _log_warn(f"{site_name}: 点击每日签到按钮失败(可能已签到): {e2}")
                                if clicked:
                                    _wait_for_result(site_name, max_wait=20)
                            elif "audiences" in site_url_lower or "观众" in site_name:
                                # 观众：先检查是否已签到（attendance-card--done），未签到则点击人机验证DIV
                                try:
                                    already_done = page.evaluate("""
                                        (function() {
                                            var doneCard = document.querySelector('.attendance-card--done');
                                            if (doneCard) return true;
                                            return false;
                                        })();
                                    """)
                                    if already_done:
                                        _log(f"{site_name}: 检测到已签到（attendance-card--done）")
                                        return
                                except:
                                    pass
                                # 点击"人机验证"DIV（class: attendance-card--verify），然后等待CF验证通过
                                try:
                                    page.click('.attendance-card--verify', timeout=5000)
                                    _log(f"{site_name}: 已点击人机验证DIV(.attendance-card--verify)")
                                except:
                                    try:
                                        page.click('text=人机验证', timeout=5000)
                                        _log(f"{site_name}: 已点击人机验证(text选择器)")
                                    except Exception as e2:
                                        _log_warn(f"{site_name}: 点击人机验证失败: {e2}")
                                # 观众CF验证较慢，等待60秒
                                _wait_for_result(site_name, max_wait=60)
                            elif "ourbits" in site_url_lower or "我堡" in site_name:
                                # 我堡：点击签到按钮，然后等待CF验证和签到结果
                                clicked = False
                                try:
                                    page.click('text=签到', timeout=5000)
                                    _log(f"{site_name}: 已点击签到按钮")
                                    clicked = True
                                except:
                                    try:
                                        page.click('a:has-text("签到")', timeout=5000)
                                        _log(f"{site_name}: 已点击签到链接")
                                        clicked = True
                                    except Exception as e2:
                                        _log_warn(f"{site_name}: 点击签到按钮失败（可能已签到）: {e2}")
                                if clicked:
                                    # 我堡点击后需要等待CF五秒盾验证通过
                                    _wait_for_result(site_name, max_wait=30)
                            
                            # 等待签到完成
                            try:
                                page.wait_for_load_state("networkidle", timeout=10000)
                            except:
                                pass
                            import time as _t
                            _t.sleep(2)
                            
                            # 返回点击后的页面源码
                            return page.content()
                        
                        page_source = PlaywrightHelper().action(
                            url=auto_click_url,
                            callback=_auto_click_sign,
                            cookies=site_cookie,
                            ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            timeout=90
                        )
                        if page_source and len(page_source) > 0:
                            _log(f"{site_name}: 自动点击签到按钮成功，页面长度={len(page_source)}")
                        else:
                            _log_warn(f"{site_name}: 自动点击签到按钮后页面为空")
                    except Exception as e:
                        _log_warn(f"{site_name}: 自动点击签到按钮异常: {e}")

                # 标记：自动点击是否已成功获取页面（避免后续requests/Playwright覆盖结果）
                auto_click_done = bool(page_source and len(page_source) > 500)
                if auto_click_done:
                    _log(f"{site_name}: 自动点击已获取签到结果页面，跳过后续请求降级")

                # 春天/朋友站点：无签到按钮，直接用Playwright访问主页判断登录状态（登录保号）
                no_sign_button_site = (
                    ("springsunday" in site_url_lower or "春天" in site_name) or
                    ("keepfrds" in site_url_lower or "朋友" in site_name)
                )
                if no_sign_button_site and not auto_click_done:
                    _log(f"{site_name}: 无签到按钮站点，直接Playwright访问主页判断登录状态...")
                    try:
                        home_url = f"{site_url.rstrip('/')}/index.php"
                        pw_source = PlaywrightHelper().get_page_source(
                            url=home_url,
                            cookies=site_cookie,
                            ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            timeout=30
                        )
                        if pw_source and len(pw_source) > 500:
                            page_source = pw_source
                            auto_click_done = True
                            _log(f"{site_name}: Playwright获取主页成功，长度={len(page_source)}")
                        else:
                            _log_warn(f"{site_name}: Playwright获取主页失败或页面过短")
                    except Exception as e:
                        _log_warn(f"{site_name}: Playwright访问主页异常: {e}")

                # 第1步：普通请求（最快），只使用硬编码的签到URL，不再试探其他URL
                fallback_urls = [sign_url]
                for try_url in fallback_urls:
                    if auto_click_done:
                        break
                    try:
                        # 改用requests库直接请求，绕过MP RequestUtils的headers问题（User-Agent=None导致站点拒绝）
                        import requests as _requests_lib
                        _headers = {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                        }
                        if site_cookie:
                            _headers['Cookie'] = site_cookie
                        resp = _requests_lib.get(try_url, headers=_headers, timeout=30, verify=False, proxies=_proxies)
                        resp_text = resp.content.decode('utf-8', errors='replace') if resp else ""
                        if resp and resp.status_code == 200 and resp_text and len(resp_text) > 100:
                            # 检查是否是404页面或空页面
                            if '404' in resp_text[:500] and 'Not Found' in resp_text[:500]:
                                _log_warn(f"{site_name}: URL返回404: {try_url}")
                                continue
                            page_source = resp_text
                            if try_url != sign_url:
                                _log(f"{site_name}: fallback成功，使用URL: {try_url}")
                            break
                        else:
                            status = resp.status_code if resp else "无响应"
                            _log_warn(f"{site_name}: URL请求失败(状态码:{status}): {try_url}")
                    except Exception as e:
                        _log_warn(f"{site_name}: URL请求异常: {try_url}, 错误: {e}")

                # 检查是否所有URL都失败了
                all_urls_failed = not page_source or len(page_source) < 50
                
                # 检查页面是否包含403/雷池/安全验证/CF5秒盾关键词
                has_403_block = False
                if page_source:
                    block_keywords = ['403', 'Forbidden', '雷池', '安全验证', '正在验证', '访问被拒绝', '请求被拦截', 'WAF', 'Web Application Firewall',
                                      # CF5秒盾关键词（我堡、观众等站点）
                                      '请耐心等待', '验证通过后将自动完成签到', '签到验证程序加载',
                                      'Just a moment', 'Checking your browser', 'cf-challenge', 'under_challenge',
                                      'cloudflare', 'Attention Required']
                    has_403_block = any(kw.lower() in page_source.lower() for kw in block_keywords)
                    if has_403_block:
                        _log(f"{site_name}: 检测到403/雷池/安全验证/CF5秒盾页面，准备降级浏览器仿真")
                
                # 第1.5步：如果站点开启了浏览器仿真(render=True)，或者检测到403/雷池，直接用Playwright（自动点击成功则跳过）
                if not auto_click_done and (site_render or has_403_block):
                    _log(f"{site_name}: 站点render={site_render}, 403拦截={has_403_block}，使用Playwright浏览器仿真")
                    page_source = None
                    try:
                        # 雷池/安全验证页面需要更长等待时间（猪猪等站点雷池验证较慢）
                        pw_timeout = 120 if has_403_block else 30
                        page_source = PlaywrightHelper().get_page_source(
                            url=sign_url,
                            cookies=site_cookie,
                            ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            timeout=pw_timeout
                        )
                        if page_source:
                            _log(f"{site_name}: Playwright获取页面成功，长度={len(page_source)}")
                            # 先判断是否是CF拦截页面（排除CF拦截，避免误判为雷池验证）
                            is_cf_block = ("Attention Required" in page_source or "cf-error" in page_source or
                                           "you have been blocked" in page_source.lower() or
                                           "cloudflare" in page_source.lower()[:500])
                            # 如果页面还是雷池验证页面（非CF拦截），等待10秒后重试1次
                            leichi_keywords = ['雷池', '安全验证', '正在验证', '请稍候', '访问被拒绝', '请求被拦截']
                            if not is_cf_block and any(kw in page_source for kw in leichi_keywords) and len(page_source) < 50000:
                                _log(f"{site_name}: 页面仍为雷池验证页面，等待10秒后重试...")
                                import time as _time
                                _time.sleep(10)
                                retry_source = PlaywrightHelper().get_page_source(
                                    url=sign_url,
                                    cookies=site_cookie,
                                    ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                    timeout=pw_timeout
                                )
                                if retry_source and len(retry_source) > len(page_source):
                                    page_source = retry_source
                                    _log(f"{site_name}: 重试成功，页面长度={len(page_source)}")
                    except Exception as e:
                        _log_warn(f"{site_name}: Playwright失败: {e}")

                # 第2步：检测CF挑战，降级FlareSolverr
                if page_source and under_challenge(page_source) and self._auto_cf >= 1:
                    _log(f"{site_name}: 检测到CF挑战，降级FlareSolverr")
                    page_source = None
