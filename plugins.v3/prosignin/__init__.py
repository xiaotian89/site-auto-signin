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
                        _log_warn(f"{site_name}: FlareSolverr失败: {e}")

                # 第3步：仍有CF挑战，降级Playwright
                if page_source and under_challenge(page_source) and self._auto_cf >= 2:
                    _log(f"{site_name}: FlareSolverr未过CF，降级Playwright")
                    page_source = None
                    try:
                        page_source = PlaywrightHelper().get_page_source(
                            url=sign_url,
                            cookies=site_cookie,
                            ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            timeout=20
                        )
                    except Exception as e:
                        _log_warn(f"{site_name}: Playwright失败: {e}")

                # 第3.5步：检测站点自有滑块验证码，自动拖动
                # 先严格排除CF挑战页面，避免CF五秒盾被误判为滑块
                is_cf = under_challenge(page_source) if page_source else False
                if not is_cf and page_source:
                    is_cf = self.__is_cf_page(page_source)
                
                if page_source and not is_cf:
                    # 检查页面是否包含滑块关键词（52PT的"请先完成滑块"也能匹配，农场的"拖动滑块验证"也能匹配）
                    slider_keywords = ['滑块', '滑动验证', '拖动验证', 'slider-btn', 'slider_container', 'geetest', 'nc_iconfont', '拖动滑块', 'slide-to-verify', '请先完成滑块', '滑动认证']
                    has_slider = any(kw.lower() in page_source.lower() for kw in slider_keywords)
                    # 52PT、农场等站点只要检测到滑块就处理，不强制要求_auto_cf>=1
                    if has_slider and (self._auto_cf >= 1 or '52pt' in site_name.lower() or '52pt' in site_url_lower or '0ff' in site_url_lower or '农场' in site_name):
                        _log(f"{site_name}: 检测到站点自有滑块验证码，尝试自动拖动")
                        try:
                            slider_result = self.__handle_site_slider(sign_url, site_name, site_cookie)
                            if slider_result:
                                page_source = slider_result
                                _log(f"{site_name}: 滑块处理完成，页面长度={len(page_source)}")
                            else:
                                _log_warn(f"{site_name}: 滑块处理返回空结果")
                        except Exception as e:
                            _log_warn(f"{site_name}: 滑块自动处理失败: {e}")

                # 检查是否是404页面（签到页面不存在）
                elif page_source and ("File not found" in page_source or "404 Not Found" in page_source) and len(page_source) < 500:
                    result = f"❌ {site_name}: 无签到功能(签到页面不存在)"
                if not page_source:
                    _log_warn(f"{site_name}: 请求失败，page_source为空，URL: {sign_url}")
                    result = f"❌ {site_name}: 请求失败"
                elif under_challenge(page_source):
                    result = f"⚠️ {site_name}: CF挑战(请开启浏览器仿真)"
                elif "Attention Required" in page_source or "cf-error" in page_source or "you have been blocked" in page_source.lower() or "cloudflare" in page_source.lower()[:500]:
                    # Cloudflare拦截页面（IP被封或WAF拦截，非标准5秒盾）
                    if site_render:
                        result = f"⚠️ {site_name}: CF拦截(浏览器仿真已开启但IP被封，请浏览器手动过验证后更新Cookie)"
                    else:
                        result = f"⚠️ {site_name}: CF拦截(请开启浏览器仿真或更新Cookie)"
                elif "二级密码" in page_source or "二级验证" in page_source or "请输入二级密码" in page_source or "安全密码" in page_source or "交易密码" in page_source:
                    # 二级密码验证页面（如猪猪等站点，需要手动输入二级密码，无法自动签到）
                    result = f"⚠️ {site_name}: 需要二级密码验证(无法自动签到，请浏览器手动签到)"
                elif "已签到" in page_source or "今日已签到" in page_source or "已经签到" in page_source or "请勿重复签到" in page_source or "今天已签" in page_source or "您今天已经签到" in page_source or "已簽到" in page_source or "今日已簽到" in page_source or "已經簽到" in page_source or "請勿重複簽到" in page_source:
                    # 已签到（优先于验证码判断，手动签到后页面可能同时包含验证码和已签到）
                    # 高清视界特殊处理：页面同时包含红色"[签到]"和绿色"[已签到]"，通过display:none切换
                    # 只有绿色的"[已签到]"（<font color="green">[已签到]</font>）才表示真正已签到
                    if "hdarea" in site_url_lower or "高清视界" in site_name:
                        if 'color="green">[已签到]' in page_source or "color='green'>[已签到]" in page_source:
                            result = f"✅ {site_name}: 已签到"
                        else:
                            # 红色"[签到]"可见，说明还没签到，需要点击签到按钮
                            result = f"⚠️ {site_name}: 需要点击签到按钮(页面显示签到按钮，未自动签到)"
                    else:
                        result = f"✅ {site_name}: 已签到"
                elif not SiteUtils.is_logged_in(page_source):
                    # Cookie失效（优先于图形验证码判断，登录页面通常包含"验证码"关键词，避免误判）
                    result = f"❌ {site_name}: Cookie失效"
                elif "请耐心等待" in page_source or "验证通过后将自动完成签到" in page_source or "签到验证程序加载" in page_source or "Just a moment" in page_source or "Checking your browser" in page_source or "cf-challenge" in page_source.lower() or "under_challenge" in page_source.lower():
                    # CF5秒盾页面（我堡、观众等站点），需要浏览器仿真等待验证通过
                    if site_render:
                        result = f"⚠️ {site_name}: CF5秒盾验证中(浏览器仿真已开启，等待超时)"
                    else:
                        result = f"⚠️ {site_name}: CF5秒盾验证(请在站点管理开启浏览器仿真)"
                elif any(kw in site_name or kw in site_url_lower for kw in ['hdsky', '天空', '皇后', 'queen']) and ("验证码" in page_source or "captcha" in page_source.lower() or "verification code" in page_source.lower() or "verifycode" in page_source.lower() or "请输入验证码" in page_source or "图形验证码" in page_source or "字符验证码" in page_source):
                    # 图形验证码页面（仅对天空、皇后等已知需要图形验证码的站点判断，避免其他站点误判）
                    result = f"⚠️ {site_name}: 需要输入图形验证码(无法自动签到，请浏览器手动签到)"
                elif "雷池" in page_source or "安全验证" in page_source or "正在验证" in page_source or "请稍候" in page_source:
                    # 雷池安全验证页面，需要浏览器渲染等待
                    if site_render:
                        result = f"⚠️ {site_name}: 雷池验证中(浏览器仿真已开启，等待超时)"
                    else:
                        result = f"⚠️ {site_name}: 雷池验证(请在站点管理开启浏览器仿真)"
                elif not SiteUtils.is_logged_in(page_source):
                    result = f"❌ {site_name}: Cookie失效"
                elif "springsunday" in site_url_lower or "春天" in site_name or "keepfrds" in site_url_lower or "朋友" in site_name:
                    # 春天/朋友站点：无签到按钮，登录成功即保号
                    if page_source and SiteUtils.is_logged_in(page_source):
                        result = f"✅ {site_name}: 无签到按钮(登录保号)"
                    else:
                        result = f"❌ {site_name}: 无法登录，无法保号"
                elif ("hdarea" in site_url_lower or "高清视界" in site_name) and '<font color="red">[签到]</font>' in page_source:
                    # 高清视界：页面同时包含红色[签到]按钮和绿色[已签到]文字，通过JS切换显示
                    # 用requests获取页面不会执行JS，无法判断哪个显示；只要有红色[签到]按钮就说明需要点击
                    result = f"⚠️ {site_name}: 需要点击签到按钮(无法自动签到，请浏览器手动签到)"
                elif ("btschool" in site_url_lower or "学校" in site_name) and ("每日签到" in page_source or "action=addbonus" in page_source):
                    # 学校：页面有"每日签到"按钮，需要点击才能签到
                    result = f"⚠️ {site_name}: 需要点击签到按钮(无法自动签到，请浏览器手动签到)"
                elif "签到成功" in page_source or "签到完成" in page_source or "成功签到" in page_source or "签到奖励" in page_source or "获得魔力" in page_source or "魔力+" in page_source or "打卡成功" in page_source or "今日签到" in page_source or "签到已完成" in page_source or "簽到成功" in page_source or "簽到完成" in page_source or "簽到獎勵" in page_source or "獲得魔力" in page_source or "签到已得" in page_source or "簽到已得" in page_source or "查看签到记录" in page_source or "查看簽到記錄" in page_source or SiteUtils.is_checkin(page_source):
                    result = f"✅ {site_name}: 签到成功"
                else:
                    # 兜底：请求已发送但未匹配到明确成功/失败关键词，结果未知
                    result = f"⚠️ {site_name}: 登录成功，签到未确认"

                # 重试判断：如果结果包含重试关键词，且不是最后一次尝试
                if attempt < max_retries and retry_keywords:
                    if any(kw in result for kw in retry_keywords):
                        _log(f"{site_name}: 结果命中重试关键词，准备重试")
                        continue
                return result

            except Exception as e:
                _log_error(f"{site.name}: 异常 {e}")
                if attempt < max_retries:
                    continue
                return f"❌ {site.name}: 异常 {str(e)[:50]}"

        return f"❌ {site.name}: 重试次数耗尽"


    def __signin_mteam(self, site, site_name, site_url):
        """馒头(m-team) API签到：更新最后访问时间保号，实际没有签到按钮"""
        try:
            token = getattr(site, "token", "") or getattr(site, "apikey", "") or ""
            ua = getattr(site, "ua", "") or "Mozilla/5.0"
            timeout = getattr(site, "timeout", 30) or 30
            proxy = getattr(site, "proxy", None)

            # 构建代理配置（容器默认代理 http://192.168.2.70:7892）
            import os as _os
            proxies = None
            if proxy:
                proxy_url = _os.environ.get('HTTP_PROXY') or _os.environ.get('HTTPS_PROXY') or 'http://192.168.2.70:7892'
                proxies = {'http': proxy_url, 'https': proxy_url}

            from urllib.parse import urlparse
            domain = urlparse(site_url).netloc
            if domain.startswith("www."):
                domain = domain[4:]
            # 馒头API地址特殊处理：kp.m-team.cc的API是api.m-team.cc，不是api.kp.m-team.cc
            if 'm-team.cc' in domain:
                api_domain = 'api.m-team.cc'
            else:
                api_domain = f'api.{domain}'

            headers = {
                "Content-Type": "application/json",
                "User-Agent": ua,
                "Accept": "application/json, text/plain, */*",
                "Authorization": str(token).strip()
            }

            import requests as _requests_lib
            res = _requests_lib.post(
                url=f"https://{api_domain}/api/member/updateLastBrowse",
                headers=headers, timeout=timeout, verify=False, proxies=proxies)
            
            if res and res.status_code in (200, 301, 302):
                try:
                    payload = res.json()
                    if isinstance(payload, dict) and str(payload.get("code")) == "0":
                        return f"✅ {site_name}: 保号成功(更新访问时间)"
                except Exception:
                    pass
                if res.status_code in (301, 302):
                    return f"✅ {site_name}: 保号成功(重定向)"
                return f"✅ {site_name}: 保号成功"
            elif res:
                return f"❌ {site_name}: 保号失败(状态码:{res.status_code})"
            else:
                return f"❌ {site_name}: 保号失败(无法连接)"
        except Exception as e:
            return f"❌ {site_name}: 保号异常: {str(e)[:50]}"

    def __signin_rousi(self, site, site_name, site_url):
        """肉丝(rousi.pro) PeerGo系统 API Key签到，参考MP内置实现"""
        try:
            apikey = str(getattr(site, "apikey", "") or "").strip()
            token = str(getattr(site, "token", "") or "").strip()
            ua = getattr(site, "ua", "") or "Mozilla/5.0"
            timeout = getattr(site, "timeout", 30) or 30
            proxy = getattr(site, "proxy", None)

            # 构建代理配置
            import os as _os
            proxies = None
            if proxy:
                proxy_url = _os.environ.get('HTTP_PROXY') or _os.environ.get('HTTPS_PROXY') or 'http://192.168.2.70:7892'
                proxies = {'http': proxy_url, 'https': proxy_url}

            if not apikey and not token:
                return f"❌ {site_name}: 缺少API Key"

            base_headers = {
                "Content-Type": "application/json",
                "User-Agent": ua,
                "Accept": "application/json, text/plain, */*"
            }
            body = {"mode": "fixed"}

            api_url = f"{site_url.rstrip('/')}/api/points/attendance"
            res = None

            # 优先用 api-token header（个人API Key）
            if apikey:
                import requests as _requests_lib
                res = _requests_lib.post(
                    url=api_url,
                    headers={**base_headers, "api-token": apikey},
                    json=body, timeout=timeout, verify=False, proxies=proxies)
                
                # 检查是否成功
                if res and res.status_code == 200:
                    try:
                        payload = res.json() or {}
                        if payload.get("code") == 0:
                            return f"✅ {site_name}: 签到成功"
                    except Exception:
                        pass
                # 检查是否已签到
                if res and res.status_code == 400:
                    try:
                        payload = res.json() or {}
                        code = payload.get("code")
                        msg = payload.get("message") or payload.get("msg") or ""
                        if code == 1 and ("已签到" in msg or "重复" in msg or "already" in str(msg).lower()):
                            return f"✅ {site_name}: 已签到"
                    except Exception:
                        pass
                # api-token失败，回退Authorization
                if token:
                    res = None
            
            # 回退用 Authorization: Bearer
            if token and res is None:
                auth_value = token if token.lower().startswith("bearer ") else f"Bearer {token}"
                import requests as _requests_lib
                res = _requests_lib.post(
                    url=api_url,
                    headers={**base_headers, "Authorization": auth_value},
                    json=body, timeout=timeout, verify=False, proxies=proxies)
            
            # 最终判定
            if res and res.status_code == 200:
                try:
                    payload = res.json() or {}
                    if payload.get("code") == 0:
                        return f"✅ {site_name}: 签到成功"
                except Exception:
                    pass
                return f"✅ {site_name}: 签到成功"
            elif res and res.status_code == 400:
                try:
                    payload = res.json() or {}
                    code = payload.get("code")
                    msg = payload.get("message") or payload.get("msg") or ""
                    if code == 1 and ("已签到" in msg or "重复" in msg or "already" in str(msg).lower()):
                        return f"✅ {site_name}: 已签到"
                except Exception:
                    pass
                return f"❌ {site_name}: 签到失败(状态码:400)"
            elif res and res.status_code in (401, 403):
                return f"❌ {site_name}: API Key已失效或权限不足"
            elif res:
                return f"❌ {site_name}: 签到失败(状态码:{res.status_code})"
            else:
                return f"❌ {site_name}: 签到失败(无法连接)"
        except Exception as e:
            return f"❌ {site_name}: 签到异常: {str(e)[:50]}"

    def __signin_zhuque(self, site, site_name, site_url):
        """朱雀(zhuque.in) 释放技能游戏化签到（需要cookie）"""
        try:
            site_cookie = getattr(site, "cookie", "") or ""
            if not site_cookie:
                return f"❌ {site_name}: 无Cookie"
            ua = getattr(site, "ua", "") or "Mozilla/5.0"
            timeout = getattr(site, "timeout", 30) or 30
            
            # 1. 获取页面，提取 x-csrf-token
            import requests as _requests_lib
            _get_headers = {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Cookie": site_cookie,
            }
            page_res = _requests_lib.get(url="https://zhuque.in",
                headers=_get_headers, timeout=timeout, verify=False)
            if not page_res or page_res.status_code != 200:
                return f"❌ {site_name}: 无法连接"
            
            html_text = page_res.content.decode('utf-8', errors='replace')
            if "login.php" in html_text:
                return f"❌ {site_name}: Cookie失效"
            
            # 提取 x-csrf-token
            import re
            csrf_match = re.search(r'name="x-csrf-token"\s+content="([^"]+)"', html_text)
            if not csrf_match:
                csrf_match = re.search(r'<meta[^>]+x-csrf-token[^>]+content="([^"]+)"', html_text)
            if not csrf_match:
                return f"❌ {site_name}: 未找到csrf-token"
            
            csrf_token = csrf_match.group(1)
            
            # 2. 释放技能
            headers = {
                "x-csrf-token": str(csrf_token),
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": ua
            }
            skill_headers = {
                "x-csrf-token": str(csrf_token),
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": ua,
                "Cookie": site_cookie,
            }
            data = {"all": 1, "resetModal": "true"}
            
            import requests as _requests_lib
            skill_res = _requests_lib.post(
                url="https://zhuque.in/api/gaming/fireGenshinCharacterMagic",
                headers=skill_headers, json=data, timeout=timeout, verify=False)
            
            if skill_res and skill_res.status_code == 200:
                try:
                    skill_dict = skill_res.json()
                    if skill_dict.get('status') == 200:
                        bonus = skill_dict.get('data', {}).get('bonus', 0)
                        return f"✅ {site_name}: 释放技能成功(+{bonus}魔力)"
                except Exception:
                    pass
                return f"✅ {site_name}: 释放技能成功"
            elif skill_res:
                return f"❌ {site_name}: 释放技能失败(状态码:{skill_res.status_code})"
            else:
                return f"❌ {site_name}: 释放技能失败(无法连接)"
        except Exception as e:
            return f"❌ {site_name}: 释放技能异常: {str(e)[:50]}"

    def __signin_ttg(self, site, site_name, site_url):
        """听听歌(totheglory.im) AJAX签到：先GET页面提取timestamp和token，再POST signed.php"""
        try:
            import re as _re
            import requests as _requests_lib
            import warnings as _warnings
            _warnings.filterwarnings('ignore')

            site_cookie = getattr(site, "cookie", "") or ""
            if not site_cookie:
                return f"❌ {site_name}: 无Cookie"
            ua = getattr(site, "ua", "") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            timeout = getattr(site, "timeout", 30) or 30

            base_headers = {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": site_cookie,
            }

            # 第1步：GET签到页面，提取 signed_timestamp 和 signed_token
            page_url = f"{site_url.rstrip('/')}/plugin.php?id=sign"
            page_res = _requests_lib.get(page_url, headers=base_headers, timeout=timeout, verify=False)
            if page_res.status_code != 200:
                return f"❌ {site_name}: 获取签到页面失败(状态码:{page_res.status_code})"

            html = page_res.content.decode('utf-8', errors='replace')

            # 检查是否已签到（页面显示"已签到"）
            if '已签到' in html and 'a#signed' not in html:
                return f"✅ {site_name}: 已签到"

            # 提取 timestamp 和 token
            ts_match = _re.search(r'signed_timestamp\s*:\s*["\'](\d+)["\']', html)
            token_match = _re.search(r'signed_token\s*:\s*["\']([a-f0-9]+)["\']', html)

            if not ts_match or not token_match:
                # 可能已经签到了，检查页面内容
                if '已签到' in html:
                    return f"✅ {site_name}: 已签到"
                return f"❌ {site_name}: 未找到签到参数(timestamp/token)"

            signed_ts = ts_match.group(1)
            signed_token = token_match.group(1)

            # 第2步：POST签到
            sign_url = f"{site_url.rstrip('/')}/signed.php"
            post_headers = {
                **base_headers,
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": page_url,
            }
            post_data = {
                "signed_timestamp": signed_ts,
                "signed_token": signed_token,
            }

            sign_res = _requests_lib.post(sign_url, headers=post_headers, data=post_data,
                                           timeout=timeout, verify=False)

            if sign_res.status_code == 200:
                result_text = sign_res.content.decode('utf-8', errors='replace').strip()
                # 签到成功的返回通常包含"成功"、"签到"、魔力值等，或者空字符串
                if any(kw in result_text for kw in ['成功', '签到', '魔力', 'bonus', '已签']) or not result_text:
                    return f"✅ {site_name}: 签到成功"
                elif '已签' in result_text or '重复' in result_text:
                    return f"✅ {site_name}: 已签到"
                else:
                    # 返回内容可能是提示信息，也算成功（因为状态码200）
                    return f"✅ {site_name}: 签到成功({result_text[:30]})"
            elif sign_res.status_code in (301, 302):
                return f"✅ {site_name}: 签到成功(重定向)"
            else:
                return f"❌ {site_name}: 签到失败(状态码:{sign_res.status_code})"
        except Exception as e:
            return f"❌ {site_name}: 签到异常: {str(e)[:50]}"

    @staticmethod
    def __is_cf_page(page_source):
        """更全面的CF挑战页面检测，避免误判为站点自有滑块"""
        if not page_source:
            return False
        page_lower = page_source.lower()
        # CF 特有关键词（标题、正文、JS变量）
        cf_keywords = [
            'just a moment', '请稍候', 'checking your browser',
            'cloudflare', 'ray id', 'ray_id', 'cf-challenge',
            'cf-please-wait', 'challenge-spinner', 'attack-box',
            'attention required', 'security check', 'verify you are human',
            'trk_jschal', 'jschl_vc', 'jschl_answer',
            'ddos-guard', 'ddos protection',
            'captcha-bypass', 'cf-turnstile',
        ]
        for kw in cf_keywords:
            if kw in page_lower:
                return True
        return False

    def __handle_site_slider(self, sign_url, site_name, site_cookie):
        """处理站点自有滑块验证码，用MP内置PlaywrightHelper"""
        import time as _t
        import random as _r
        
        def slider_callback(page):
            """滑块拖动回调函数"""
            _t.sleep(1.5)
            
            # 52PT专门处理
            is_52pt = '52pt' in site_name.lower() or '52pt' in sign_url.lower()
            # 农场专门处理（滑块元素ID: dragHandler）
            is_farm = '0ff' in site_url_lower or '农场' in site_name
            
            if is_52pt:
                return self.__handle_52pt_slider(page, site_name)
            
            if is_farm:
                return self.__handle_farm_slider(page, site_name)
            
            # 通用滑块处理
            slider_selectors = [
                '#slider-btn', '#slider', '.slider-btn', '.slide-btn',
                '.drag-btn', '.nc_iconfont.btn_slide', '.geetest_slider_button',
                '[class*="slider"]', '[class*="slide"]', '[class*="drag"]',
            ]
            
            slider_btn = None
            for selector in slider_selectors:
                try:
                    elements = page.query_selector_all(selector)
                    if elements:
                        for el in elements:
                            if el.is_visible() and el.is_enabled():
                                slider_btn = el
                                break
                        if slider_btn:
                            break
                except Exception:
                    continue
            
            if not slider_btn:
                _log_warn(f"{site_name}: 未找到滑块按钮元素")
                return page.content()
            
            try:
                btn_box = slider_btn.bounding_box()
                if not btn_box:
                    return page.content()
                
                container_width = btn_box['width'] * 5
                for container_sel in ['#slider-container', '.slider-container', '[class*="slider-container"]']:
                    container = page.query_selector(container_sel)
                    if container:
                        c_box = container.bounding_box()
                        if c_box:
                            container_width = c_box['width']
                            break
                
                start_x = btn_box['x'] + btn_box['width'] / 2
                start_y = btn_box['y'] + btn_box['height'] / 2
                target_x = btn_box['x'] + container_width - btn_box['width'] - 5
                target_y = start_y + _r.uniform(-2, 2)
                
                mouse = page.mouse
                mouse.move(start_x, start_y)
                _t.sleep(_r.uniform(0.1, 0.3))
                mouse.down()
                _t.sleep(_r.uniform(0.1, 0.2))
                
                steps = _r.randint(15, 25)
                for i in range(1, steps + 1):
                    progress = i / steps
                    eased_progress = 1 - (1 - progress) ** 2
                    current_x = start_x + (target_x - start_x) * eased_progress
                    current_y = start_y + _r.uniform(-3, 3)
                    mouse.move(current_x, current_y)
                    if i > steps * 0.7:
                        _t.sleep(_r.uniform(0.02, 0.08))
                    else:
                        _t.sleep(_r.uniform(0.01, 0.03))
                
                mouse.move(target_x, target_y)
                _t.sleep(_r.uniform(0.1, 0.3))
                mouse.up()
                _t.sleep(_r.uniform(0.5, 1.0))
                
                return page.content()
            except Exception as e:
                _log_warn(f"{site_name}: 滑块拖动异常: {e}")
                return page.content()
        
        # 用 PlaywrightHelper.action() 执行滑块操作
        try:
            result = PlaywrightHelper().action(
                url=sign_url,
                callback=slider_callback,
                cookies=site_cookie,
                ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                headless=True,
                timeout=45
            )
            return result
        except Exception as e:
            _log_warn(f"{site_name}: PlaywrightHelper滑块处理失败: {e}")
            return None
    
    def __handle_52pt_slider(self, page, site_name):
        """52PT专门滑块处理：先从主页点击签到赚魔力进入（有Referer检测），再用JS设置滑块位置+提交"""
        import time as _t
        
        try:
            # 检查当前页面是否显示"暂停使用"（直接访问签到页面会触发Referer检测）
            page_content = page.content()
            if '暂停使用' in page_content or '签到页面已暂停' in page_content:
                _log(f"{site_name}: 检测到Referer拦截，先从主页进入...")
                # 先访问主页
                page.goto('https://52pt.site/index.php', wait_until='networkidle', timeout=15000)
                _t.sleep(1)
                # 点击"签到赚魔力"链接
                try:
                    sign_link = page.query_selector('a#game')
                    if sign_link:
                        sign_link.click()
                        _t.sleep(2)
                        page.wait_for_load_state('networkidle', timeout=10000)
                        _log(f"{site_name}: 已从主页点击签到赚魔力进入签到页面")
                    else:
                        # 找不到链接，直接导航到签到页面（带上Referer）
                        page.goto('https://52pt.site/52bakatestdate0823.php', wait_until='networkidle', timeout=15000)
                except Exception as e:
                    _log_warn(f"{site_name}: 点击签到赚魔力失败，直接导航: {e}")
                    page.goto('https://52pt.site/52bakatestdate0823.php', wait_until='networkidle', timeout=15000)
                _t.sleep(1)
            
            # 等待滑块加载
            page.wait_for_selector('#slider-btn', timeout=10000)
            _t.sleep(0.5)
            
            # 用JS直接完成滑块验证（52PT没有轨迹检测，已验证可行）
            js_slider_code = """
            (function() {
                var slider = document.getElementById('slider-btn');
                var bg = document.getElementById('slider-bg');
                var container = document.getElementById('slider-container');
                var submitBtn = document.getElementById('submit-btn');
                
                if (!slider || !container) {
                    return 'slider elements not found';
                }
                
                // 如果已经完成，直接返回
                if (submitBtn && !submitBtn.disabled) {
                    return 'already completed';
                }
                
                var maxLeft = container.offsetWidth - 54;
                var btnRect = slider.getBoundingClientRect();
                var containerRect = container.getBoundingClientRect();
                
                // 1. 触发mousedown事件
                var mouseDownEvent = new MouseEvent('mousedown', {
                    bubbles: true, cancelable: true,
                    clientX: btnRect.left + 25, clientY: btnRect.top + 18
                });
                slider.dispatchEvent(mouseDownEvent);
                
                // 2. 设置滑块位置（拖到最右边）
                slider.style.left = maxLeft + 'px';
                bg.style.width = (maxLeft + 25) + 'px';
                
                // 3. 触发mousemove事件
                var mouseMoveEvent = new MouseEvent('mousemove', {
                    bubbles: true, cancelable: true,
                    clientX: containerRect.left + maxLeft + 25, clientY: btnRect.top + 18
                });
                document.dispatchEvent(mouseMoveEvent);
                
                // 4. 触发mouseup事件
                var mouseUpEvent = new MouseEvent('mouseup', {
                    bubbles: true, cancelable: true,
                    clientX: containerRect.left + maxLeft + 25, clientY: btnRect.top + 18
                });
                document.dispatchEvent(mouseUpEvent);
                
                return 'slider completed: maxLeft=' + maxLeft;
            })();
            """
            
            result = page.evaluate(js_slider_code)
            _log(f"{site_name}: 52PT JS滑块验证结果: {result}")
            _t.sleep(1.0)  # 等待JS验证完成
            
            # 检查提交按钮是否启用
            submit_btn = page.query_selector('#submit-btn')
            if submit_btn:
                is_enabled = submit_btn.is_enabled()
                btn_text = submit_btn.inner_text() if hasattr(submit_btn, 'inner_text') else ''
                _log(f"{site_name}: 52PT提交按钮状态: enabled={is_enabled}, text={btn_text}")
                
                if is_enabled:
                    _log(f"{site_name}: 52PT点击提交签到按钮")
                    submit_btn.click()
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    _t.sleep(2)
                    return page.content()
                else:
                    _log_warn(f"{site_name}: 52PT提交按钮未启用，JS验证可能未生效")
            else:
                _log_warn(f"{site_name}: 52PT未找到提交按钮")
            
            return page.content()
        except Exception as e:
            _log_warn(f"{site_name}: 52PT滑块处理异常: {e}")
            return page.content()

    def __handle_farm_slider(self, page, site_name):
        """农场滑块专门处理：用真实鼠标拖动完成验证（农场有轨迹检测，JS直接设置位置不生效）"""
        import time as _t
        
        try:
            # 等待滑块加载
            page.wait_for_selector('#dragHandler', timeout=10000)
            _t.sleep(0.5)
            
            # 获取滑块和容器的位置
            handler = page.query_selector('#dragHandler')
            container = page.query_selector('#dragContainer')
            
            if not handler or not container:
                _log_warn(f"{site_name}: 农场滑块元素未找到")
                return page.content()
            
            handler_box = handler.bounding_box()
            container_box = container.bounding_box()
            
            if not handler_box or not container_box:
                _log_warn(f"{site_name}: 农场滑块位置获取失败")
                return page.content()
            
            # 计算起始和目标位置
            start_x = handler_box['x'] + handler_box['width'] / 2
            start_y = handler_box['y'] + handler_box['height'] / 2
            # 目标位置：容器最右边，留半个滑块宽度
            end_x = container_box['x'] + container_box['width'] - handler_box['width'] / 2
            end_y = start_y
            
            _log(f"{site_name}: 农场滑块拖动: ({start_x:.0f},{start_y:.0f}) -> ({end_x:.0f},{end_y:.0f})")
            
            # 用真实鼠标拖动（分多段，模拟人类行为，避免轨迹检测）
            mouse = page.mouse
            mouse.move(start_x, start_y)
            _t.sleep(0.1)
            mouse.down()
            _t.sleep(0.1)
            
            # 分多段移动，每段随机延迟
            steps = 20
            for i in range(1, steps + 1):
                cur_x = start_x + (end_x - start_x) * i / steps
                # Y轴随机小幅度抖动
                cur_y = start_y + (_t.random() - 0.5) * 4 if hasattr(_t, 'random') else start_y
                mouse.move(cur_x, cur_y)
                _t.sleep(0.02 + _t.random() * 0.03 if hasattr(_t, 'random') else 0.03)
            
            mouse.move(end_x, end_y)
            _t.sleep(0.1)
            mouse.up()
            
            _log(f"{site_name}: 农场滑块拖动完成，等待验证结果...")
            _t.sleep(3)  # 等待验证完成和页面跳转
            
            # 检查是否跳转成功
            current_url = page.url
            _log(f"{site_name}: 农场滑块验证后URL: {current_url}")
            
            return page.content()
        except Exception as e:
            _log_warn(f"{site_name}: 农场滑块处理异常: {e}")
            return page.content()

    def __clean_cache(self):
        """清理本插件本日缓存：仅清理prosignin自己的临时文件，不碰MP全局或其他插件"""
        cleaned = []
        try:
            import glob
            # 只清理本插件前缀的临时文件，绝对安全
            cache_patterns = [
                "/tmp/prosignin_*",
                "/tmp/pro_signin_*",
            ]
            for pattern in cache_patterns:
                for f in glob.glob(pattern):
                    try:
                        if os.path.isfile(f):
                            os.remove(f)
                            cleaned.append(f)
                    except Exception:
                        pass
            _log(f"清理本插件缓存完成，清理{len(cleaned)}个文件")
        except Exception as e:
            _log_warn(f"清理缓存失败: {e}")
        return cleaned

    def __load_history(self):
        """加载签到历史记录"""
        try:
            if os.path.exists(self._history_file):
                with open(self._history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            _log_warn(f"加载签到历史失败: {e}")
        return {"history": {}}

    def __save_history(self, history):
        """保存签到历史记录，只保留最近30天"""
        try:
            # 只保留最近30天
            if "history" in history:
                cutoff = (datetime.now() - _td(days=30)).strftime("%Y-%m-%d")
                history["history"] = {k: v for k, v in history["history"].items() if isinstance(k, str) and k >= cutoff}
            with open(self._history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log_warn(f"保存签到历史失败: {e}")

    def __record_results(self, results):
        """记录本次签到结果到历史"""
        history = self.__load_history()
        today = datetime.now().strftime("%Y-%m-%d")
        if "history" not in history:
            history["history"] = {}
        history["history"][today] = {}
        recorded = 0
        for result in results:
            # 解析结果格式: ✅ 站点名: 原因 或 ❌ 站点名: 原因（支持中英文冒号）
            if not result or len(result) < 2:
                continue
            status_icon = result[0]
            rest = result[1:].strip()
            # 同时支持英文冒号:和中文冒号：
            if "：" in rest:
                site_name, message = rest.split("：", 1)
            elif ":" in rest:
                site_name, message = rest.split(":", 1)
            else:
                site_name = rest
                message = ""
            site_name = site_name.strip()
            message = message.strip()
            if not site_name:
                continue
            status = "success" if status_icon == "✅" else ("warning" if status_icon == "⚠️" else "failed")
            history["history"][today][site_name] = {
                "status": status,
                "message": message
            }
            recorded += 1
        _log(f"记录历史：今天共{recorded}个站点结果")
        self.__save_history(history)
        _log(f"历史记录已保存到 {self._history_file}")

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
        """签到主入口：支持并发队列、失败重试、缓存清理"""
        sites = SiteOper().list_order_by_pri()
        if not sites:
            _log("没有配置站点，跳过签到")
            return

        # 清理本日缓存（如果开启）
        if self._clean:
            self.__clean_cache()
            self._clean = False
            self.__update_config()

        selected_ids = set(self._sign_sites or [])
        if not selected_ids or "all" in selected_ids:
            sign_sites = sites
        else:
            sign_sites = [s for s in sites if s.id in selected_ids]

        _log(f"开始签到，共{len(sign_sites)}个站点，并发数={self._queue_cnt}")
        results = []

        # 并发签到
        with ThreadPoolExecutor(max_workers=max(1, min(self._queue_cnt, 10))) as executor:
            future_map = {executor.submit(self.__sign_one_site, site): site for site in sign_sites}
            for future in as_completed(future_map):
                site = future_map[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    _log_error(f"{site.name}: 并发异常 {e}")
                    results.append(f"❌ {site.name}: 异常 {str(e)[:50]}")

        # 按站点名称排序输出
        results.sort()

        # 记录到历史
        self.__record_results(results)

        # 输出汇总
        success_cnt = sum(1 for r in results if r.startswith("✅"))
        failed_cnt = sum(1 for r in results if r.startswith("❌"))
        warning_cnt = sum(1 for r in results if r.startswith("⚠️"))
        _log(f"签到完成：成功{success_cnt}个，失败{failed_cnt}个，异常{warning_cnt}个")
        for r in results:
            _log(r)

        if self._notify:
            notify_text = "站点签到结果：\n" + "\n".join(results)
            try:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="站点自动签到Pro",
                    text=notify_text,
                )
                _log("签到结果通知已发送")
            except Exception as e:
                _log_warn(f"通知发送失败: {e}")