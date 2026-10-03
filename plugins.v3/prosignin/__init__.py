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
    plugin_version = "3.3.9"
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

        total = len(today_data)
        success = sum(1 for v in today_data.values() if v.get("status") == "success")
        failed = sum(1 for v in today_data.values() if v.get("status") == "failed")
        warning = sum(1 for v in today_data.values() if v.get("status") == "warning")
        history_days = len(history.get("history", {}))

        display_dates = []
        for i in range(6, -1, -1):
            d = (datetime.now() - _td(days=i)).strftime("%Y-%m-%d")
            display_dates.append(d)

        all_sites = set(today_data.keys())
        for d in display_dates:
            all_sites.update(history.get("history", {}).get(d, {}).keys())
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
                {'component': 'div', 'props': {'style': 'color:rgba(var(--v-theme-on-surface),.5);font-size:.72rem;margin-top:4px;'}, 'text': '站点自动签到Pro v3.1.1 · 并发队列+失败重试+智能降级 · 历史记录保存在 /config/prosignin_history.json，保留最近30天'},
            ]}
        ]
        return page

    def __sign_one_site(self, site):
        """签到单个站点，支持智能降级和重试"""
        import time as _time
        max_retries = 2
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
                if "m-team" in site_url_lower or "mteam" in site_url_lower:
                    api_result = self.__signin_mteam(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                elif "rousi" in site_url_lower:
                    api_result = self.__signin_rousi(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                elif "zhuque" in site_url_lower:
                    api_result = self.__signin_zhuque(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                
                site_cookie = site.cookie
                # 读取站点的浏览器仿真(render)配置
                site_render = getattr(site, 'render', False) or False
                if not site_cookie:
                    return f"❌ {site_name}: 无Cookie"

                # 52pt 等站点用自定义签到URL
                if '52pt' in site_url_lower or '52pt' in site_name.lower():
                    sign_url = f"{site_url.rstrip('/')}/52bakatestdate0823.php"
                elif 'pttime' in site_url_lower or 'pt时间' in site_name.lower() or 'PT时间' in site_name:
                    sign_url = f"{site_url.rstrip('/')}/attendance.php?type=list"
                else:
                    sign_url = f"{site_url.rstrip('/')}/attendance.php"
                page_source = None

                # 第1步：普通请求（最快），带URL fallback
                fallback_urls = [
                    sign_url,
                    f"{site_url.rstrip('/')}/signin.php",
                    f"{site_url.rstrip('/')}/sign.php",
                    f"{site_url.rstrip('/')}/checkin.php",
                    f"{site_url.rstrip('/')}/plugin.php?id=sign",
                    f"{site_url.rstrip('/')}/dailycheckin.php",
                    f"{site_url.rstrip('/')}/index.php?action=sign",
                ]
                for try_url in fallback_urls:
                    try:
                        resp = RequestUtils(cookies=site_cookie, timeout=15).get_res(url=try_url)
                        if resp and resp.status_code == 200 and resp.text and len(resp.text) > 100:
                            # 检查是否是404页面或空页面
                            if '404' in resp.text[:500] and 'Not Found' in resp.text[:500]:
                                _log_warn(f"{site_name}: URL返回404: {try_url}")
                                continue
                            page_source = resp.text
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
                
                # 检查页面是否包含403/雷池/安全验证关键词
                has_403_block = False
                if page_source:
                    block_keywords = ['403', 'Forbidden', '雷池', '安全验证', '正在验证', '访问被拒绝', '请求被拦截', 'WAF', 'Web Application Firewall']
                    has_403_block = any(kw.lower() in page_source.lower() for kw in block_keywords)
                    if has_403_block:
                        _log(f"{site_name}: 检测到403/雷池/安全验证页面，准备降级浏览器仿真")
                
                # 第1.5步：如果站点开启了浏览器仿真(render=True)，或者检测到403/雷池，直接用Playwright
                if site_render or has_403_block:
                    _log(f"{site_name}: 站点render={site_render}, 403拦截={has_403_block}，使用Playwright浏览器仿真")
                    page_source = None
                    try:
                        page_source = PlaywrightHelper().get_page_source(
                            url=sign_url,
                            cookies=site_cookie,
                            ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            timeout=30
                        )
                        if page_source:
                            _log(f"{site_name}: Playwright获取页面成功，长度={len(page_source)}")
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
                    # 检查页面是否包含滑块关键词（52PT的"请先完成滑块"也能匹配）
                    slider_keywords = ['滑块', '滑动验证', '拖动验证', 'slider-btn', 'slider_container', 'geetest', 'nc_iconfont', '拖动滑块', 'slide-to-verify', '请先完成滑块']
                    has_slider = any(kw.lower() in page_source.lower() for kw in slider_keywords)
                    # 52PT等站点只要检测到滑块就处理，不强制要求_auto_cf>=1
                    if has_slider and (self._auto_cf >= 1 or '52pt' in site_name.lower() or '52pt' in site_url_lower):
                        _log(f"{site_name}: 检测到站点自有滑块验证码，尝试自动拖动")
                        try:
                            from playwright.sync_api import sync_playwright
                            with sync_playwright() as p:
                                browser = p.chromium.launch(headless=True)
                                context = browser.new_context(
                                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                    viewport={"width": 1280, "height": 800}
                                )
                                # 设置cookie
                                if site_cookie:
                                    cookie_list = []
                                    for k, v in site_cookie.items():
                                        cookie_list.append({"name": k, "value": str(v), "domain": page.url.split('/')[2] if 'page' in dir() else ""})
                                page = context.new_page()
                                page.goto(sign_url, timeout=20000, wait_until="domcontentloaded")
                                page.wait_for_timeout(2000)
                                # 处理滑块
                                page_source = self.__handle_site_slider(sign_url, site_name, site_cookie)
                                page.wait_for_timeout(2000)
                                page_source = page.content()
                                browser.close()
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
                elif "雷池" in page_source or "安全验证" in page_source or "正在验证" in page_source or "请稍候" in page_source:
                    # 雷池安全验证页面，需要浏览器渲染等待
                    result = f"⚠️ {site_name}: 雷池验证(请在站点管理开启浏览器仿真)"
                elif not SiteUtils.is_logged_in(page_source):
                    result = f"❌ {site_name}: Cookie失效"
                elif "已签到" in page_source or "今日已签到" in page_source or "已经签到" in page_source or "请勿重复签到" in page_source or "今天已签" in page_source or "您今天已经签到" in page_source or "已簽到" in page_source or "今日已簽到" in page_source or "已經簽到" in page_source or "請勿重複簽到" in page_source:
                    result = f"✅ {site_name}: 已签到"
                elif "签到成功" in page_source or "签到完成" in page_source or "成功签到" in page_source or "签到奖励" in page_source or "获得魔力" in page_source or "魔力+" in page_source or "打卡成功" in page_source or "今日签到" in page_source or "签到已完成" in page_source or "簽到成功" in page_source or "簽到完成" in page_source or "簽到獎勵" in page_source or "獲得魔力" in page_source or "打卡成功" in page_source or "签到已得" in page_source or "簽到已得" in page_source or "已得" in page_source or SiteUtils.is_checkin(page_source):
                    result = f"✅ {site_name}: 签到成功"
                elif "魔力值" in page_source:
                    # "魔力值"太宽泛，很多页面本身就有魔力值显示，不能单独作为成功依据
                    # 只有同时包含签到相关关键词才认为成功
                    if any(kw in page_source for kw in ['签到', '签退', '打卡', '奖励', '获得', '增加', '魔力+']):
                        result = f"✅ {site_name}: 签到成功"
                    else:
                        result = f"⚠️ {site_name}: 登录成功，签到未确认"
                elif "请先完成滑块" in page_source or "拖动滑块" in page_source or "滑块验证" in page_source or "验证码错误" in page_source or "验证失败" in page_source or "签到失败" in page_source or "操作失败" in page_source:
                    result = f"❌ {site_name}: 签到失败"
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
            
            from urllib.parse import urlparse
            domain = urlparse(site_url).netloc
            if domain.startswith("www."):
                domain = domain[4:]
            
            headers = {
                "Content-Type": "application/json",
                "User-Agent": ua,
                "Accept": "application/json, text/plain, */*",
                "Authorization": str(token).strip()
            }
            # 始终使用代理（从环境变量获取）
            import os as _os_m
            _http_proxy = _os_m.environ.get('HTTPS_PROXY') or _os_m.environ.get('https_proxy') or _os_m.environ.get('HTTP_PROXY') or _os_m.environ.get('http_proxy')
            proxies = {'http': _http_proxy, 'https': _http_proxy} if _http_proxy else None
            
            res = RequestUtils(headers=headers, timeout=timeout, proxies=proxies,
                               referer=f"{site_url}index", allow_redirects=True).post_res(
                url=f"https://api.{domain}/api/member/updateLastBrowse")
            
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
            
            if not apikey and not token:
                return f"❌ {site_name}: 缺少API Key"
            
            base_headers = {
                "Content-Type": "application/json",
                "User-Agent": ua,
                "Accept": "application/json, text/plain, */*"
            }
            body = {"mode": "fixed"}
            # 始终使用代理
            import os as _os_r
            _http_proxy = _os_r.environ.get('HTTPS_PROXY') or _os_r.environ.get('https_proxy') or _os_r.environ.get('HTTP_PROXY') or _os_r.environ.get('http_proxy')
            proxies = {'http': _http_proxy, 'https': _http_proxy} if _http_proxy else None
            
            api_url = f"{site_url.rstrip('/')}/api/points/attendance"
            res = None
            
            # 优先用 api-token header（个人API Key）
            if apikey:
                res = RequestUtils(
                    headers={**base_headers, "api-token": apikey},
                    timeout=timeout, proxies=proxies
                ).post_res(url=api_url, json=body)
                
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
                res = RequestUtils(
                    headers={**base_headers, "Authorization": auth_value},
                    timeout=timeout, proxies=proxies
                ).post_res(url=api_url, json=body)
            
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
            proxy = getattr(site, "proxy", None)
            
            proxies = None
            if proxy:
                from app.core.config import settings
                proxies = settings.PROXY
            
            # 1. 获取页面，提取 x-csrf-token
            page_res = RequestUtils(cookies=site_cookie, ua=ua, timeout=timeout, 
                                    proxies=proxies).get_res(url="https://zhuque.in")
            if not page_res or page_res.status_code != 200:
                return f"❌ {site_name}: 无法连接"
            
            html_text = page_res.text
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
            data = {"all": 1, "resetModal": "true"}
            
            skill_res = RequestUtils(cookies=site_cookie, headers=headers, timeout=timeout,
                                     proxies=proxies).post_res(
                url="https://zhuque.in/api/gaming/fireGenshinCharacterMagic", json=data)
            
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
            
            if is_52pt:
                return self.__handle_52pt_slider(page, site_name)
            
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
        """52PT专门滑块处理：用JS直接设置滑块位置+触发事件完成验证，然后提交"""
        import time as _t
        
        try:
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
                    channel=NotificationChannel.WechatClawBot,
                    title="站点自动签到Pro",
                    text=notify_text,
                )
                _log("签到结果通知已发送")
            except Exception as e:
                _log_warn(f"通知发送失败: {e}")
