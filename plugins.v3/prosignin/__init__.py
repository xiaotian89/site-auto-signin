# -*- coding: utf-8 -*-
"""站点自动签到Pro - MoviePilot V3

自动签到MP里所有已添加站点，FlareSolverr优先过CF，失败自动降级Playwright浏览器渲染。
"""
import os
import sys as _sys
import json
import re
import random
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, List, Dict, Optional

# -- ProSignin 依赖持久化目录 --------------------------------------------------
# 容器重启会丢失 pip 安装的包，因此把 ddddocr 等依赖装在挂载卷 /config/pylibs，
# 并在导入前注入 sys.path，保证 OCR 能力在容器重建后依然可用。
_PLUGIN_LIB_DIRS = ["/config/pylibs"]
for _d in _PLUGIN_LIB_DIRS:
    try:
        if os.path.isdir(_d) and _d not in _sys.path:
            _sys.path.insert(0, _d)
    except Exception:
        pass

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


def _is_challenge_page(html):
    """判断是否为反爬/人机验证中间页（雷池、CF挑战等）。

    这类页面本身不含登录态，若用 is_logged_in() 判断会得出"Cookie失效"的
    错误结论（猪猪 piggo.me 的雷池页就栽在这里）。判定依据是页面文案而非
    HTTP 状态码，避免命中正文里的普通词汇。
    """
    if not html:
        return False
    markers = (
        "雷池", "安全验证", "正在验证", "请稍候", "访问被拒绝", "请求被拦截",
        "Just a moment", "Checking your browser", "cf-challenge",
        "Attention Required", "under_challenge",
        "请耐心等待", "验证通过后将自动完成签到", "签到验证程序加载",
    )
    low = html.lower()
    for m in markers:
        if m.lower() in low:
            return True
    return False


def _load_ocr():
    """加载 ddddocr 识别器（依赖装在挂载卷 /config/pylibs，容器重启不丢）。

    返回 DdddOcr 实例；不可用时抛出带安装指引的 RuntimeError。
    """
    for _d in _PLUGIN_LIB_DIRS:
        try:
            if os.path.isdir(_d) and _d not in _sys.path:
                _sys.path.insert(0, _d)
        except Exception:
            pass
    try:
        import ddddocr
    except Exception as e:
        raise RuntimeError(
            "ddddocr 不可用(" + str(e)[:60] + ")，请安装到挂载卷: "
            "docker exec moviepilot-v3 sh -c 'uv pip install --target /config/pylibs ddddocr'"
        )
    return ddddocr.DdddOcr(show_ad=False)

class ProSignin(_PluginBase):
    """站点自动签到Pro。"""

    plugin_name = "站点自动签到Pro"
    plugin_desc = "多站点自动签到，单线程顺序+随机间隔+失败重试，CF智能降级，站点专用签到逻辑，签到历史统计。"
    plugin_icon = "https://img.icons8.com/fluency/96/calendar.png"
    plugin_version = "3.9.14"
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
                                {'title': '每天 18:00', 'value': '0 18 * * *'},
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
                {'component': 'div', 'props': {'style': 'color:rgba(var(--v-theme-on-surface),.5);font-size:.72rem;margin-top:4px;'}, 'text': '站点自动签到Pro v3.9.1 · 单线程顺序+失败重试+智能降级 · 历史记录保存在 /config/prosignin_history.json，保留最近30天 · 已删除站点自动过滤'},
            ]}
        ]
        return page

    # 各站点签到URL硬编码映射（避免每次试探，减少Cookie过期风险）
    _SIGN_URL_MAP = {
        # 特殊签到URL
        '52pt': '52bakatestdate0823.php',
        'pttime': 'attendance.php',          # PT时间：只认签到端点(带 ?type=list 是记录列表页)
        # 需要点击签到按钮的站点（Playwright自动点击）
        'btschool': 'index.php?action=sign',
        'hdarea': 'index.php',
        'ourbits': 'attendance.php',
        'audiences': 'attendance.php',
        # 以下站点默认都是 attendance.php，显式列出便于维护
        'hdsky': 'showup.php',            # 天空：签到走 showup.php POST
        'ptchdbits': 'bakatest.php',      # 彩虹岛：问答题签到
        'open.cd': 'plugin_sign-in.php',  # 皇后：签到走 plugin_sign-in.php
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
                elif "kelu.one" in site_url_lower or "kelu" in site_name_lower or "Kelu" in site_name:
                    api_result = self.__signin_kelu(site, site_name, site_url)
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
                elif "agsvpt" in site_url_lower or "AGSVPT" in site_name:
                    api_result = self.__signin_agsvpt(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                elif "hdsky" in site_url_lower or "天空" in site_name:
                    api_result = self.__signin_hdsky(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                elif "open.cd" in site_url_lower or "皇后" in site_name:
                    api_result = self.__signin_opencd(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                elif "ptchdbits" in site_url_lower or "彩虹岛" in site_name:
                    api_result = self.__signin_ptchdbits(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                elif "hdarea" in site_url_lower or "高清视界" in site_name:
                    api_result = self.__signin_hdarea(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                elif "hhanclub" in site_url_lower or "憨憨" in site_name:
                    api_result = self.__signin_hhanclub(site, site_name, site_url)
                    _log(api_result)
                    return api_result
                elif "pttime" in site_url_lower or "PT时间" in site_name:
                    api_result = self.__signin_pttime(site, site_name, site_url)
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

                # 我堡(ourbits)：CF Turnstile 对 headless 不可过，但“今日已签”的页面无需过 Turnstile。
                # 先用 requests 直接取 attendance.php：命中“已经签到过”即判定为已签，避免假失败。
                _ob_bonus = None
                if "ourbits" in site_url_lower or "我堡" in site_name:
                    try:
                        import requests as _obreq
                        _obs = _obreq.Session()
                        _obs.headers.update({
                            "User-Agent": getattr(site, "ua", "") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                            "Accept-Language": "zh-CN,zh;q=0.9",
                            "Cookie": site_cookie or "",
                        })
                        _obr = _obs.get(sign_url, timeout=getattr(site, "timeout", 30) or 30, verify=False)
                        _obt = _obr.content.decode("utf-8", errors="replace")
                        if ("已经签到过了" in _obt or "请勿重复刷新" in _obt
                                or "今天已经签到过" in _obt or "您今天已经签到" in _obt):
                            _log(f"{site_name}: requests 预检判定今日已签到")
                            return f"✅ {site_name}: 已签到(今日已签)"
                        # 记录签到前魔力值基线：签到成功会 +10（首签）或 +1~+49（连续签到加成）。
                        # 页头格式固定为 "使用 ]: 270,522.5"，用它做增量判定，比页面文案可靠。
                        import re as _obr2
                        # 页头真实结构：<a href="mybonus.php">使用</a>]: 270,522.5
                        # "使用" 与 "]" 之间隔着 </a> 标签，所以不能写 "使用\s*\]"，
                        # 直接锚定 "]: 数字" 更稳（取第一个匹配即页头魔力值）。
                        _bm = _obr2.search(r"\]\s*:\s*([\d,]+(?:\.\d+)?)", _obt)
                        if _bm:
                            try:
                                _ob_bonus = float(_bm.group(1).replace(",", ""))
                            except Exception:
                                _ob_bonus = None
                        _log(f"{site_name}: requests 预检未发现已签标记(页面长度={len(_obt)}, 魔力值基线={_ob_bonus})，转 Playwright 过 Turnstile")
                    except Exception as _obe:
                        _log_warn(f"{site_name}: requests 预检异常: {_obe}")

                # 对于需要点击签到按钮的站点（学校、我堡、观众）直接使用Playwright自动点击
                # 高清视界改走专用API(__signin_hdarea)；观众走 Turnstile iframe 真实点击
                need_auto_click_site = (
                    ("btschool" in site_url_lower or "学校" in site_name) or
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
                                "查看簽到記錄"
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
                                # 观众(audiences.me)：Cloudflare Turnstile 交互式人机验证
                                # 勾选框在跨域 iframe(challenges.cloudflare.com) 内，必须点 iframe 内的
                                # checkbox；通过后页面的 cfCallback 会自动提交表单 #attendance-form
                                import time as _tw
                                try:
                                    if page.query_selector('.attendance-card--done'):
                                        _log(f"{site_name}: 检测到已签到（attendance-card--done）")
                                        return page.content()
                                except:
                                    pass

                                def _cf_frames():
                                    try:
                                        return [f for f in page.frames
                                                if "challenges.cloudflare.com" in (f.url or "")]
                                    except:
                                        return []

                                def _cf_token():
                                    try:
                                        return page.evaluate(
                                            "(function(){var e=document.getElementById('cf-token');"
                                            "return e?e.value:'';})()"
                                        ) or ""
                                    except:
                                        return ""

                                def _done():
                                    try:
                                        if page.query_selector('.attendance-card--done'):
                                            return True
                                        _html = page.content() or ""
                                        return any(_k in _html for _k in
                                                   ["attendance-card--done", "今日已签到", "已签到，明天再来"])
                                    except:
                                        return False

                                _tk_ok = _done()
                                if _tk_ok:
                                    _log(f"{site_name}: 开局即为已签到状态")
                                for _rnd in range(3):
                                    # 等 Turnstile iframe 出现
                                    for _i in range(10):
                                        _tw.sleep(2)
                                        if _cf_frames():
                                            break
                                    # 点 iframe 内的勾选框：Turnstile 的 checkbox 未必能直接 query 到，
                                    # 兜底点 iframe body（实测点 body 即可触发勾选）
                                    _clicked_sel = None
                                    for _f in _cf_frames():
                                        for _sel in ['input[type="checkbox"]', '.ctp-checkbox-label',
                                                     'label', '#challenge-stage', 'body']:
                                            try:
                                                _el = _f.query_selector(_sel)
                                                if _el:
                                                    _el.click(timeout=5000)
                                                    _clicked_sel = _sel
                                                    break
                                            except:
                                                continue
                                        if _clicked_sel:
                                            break
                                    _log(f"{site_name}: Turnstile 点击 selector={_clicked_sel}")
                                    # 等"已签到"页出现（提交后页面重载，cf-token 会消失，不能靠 token 判定）
                                    for _i in range(20):
                                        _tw.sleep(3)
                                        if _done():
                                            _log(f"{site_name}: 检测到已签到（Turnstile通过并提交成功）")
                                            _tk_ok = True
                                            break
                                    if _tk_ok:
                                        break
                                    try:
                                        page.screenshot(path=f"/tmp/aud_tk_fail_{_rnd + 1}.png")
                                        _log(f"{site_name}: 已保存Turnstile失败截图 /tmp/aud_tk_fail_{_rnd + 1}.png")
                                    except:
                                        pass
                                    _log_warn(f"{site_name}: Turnstile 未通过，第{_rnd + 1}次刷新重试")
                                    try:
                                        page.goto(auto_click_url, wait_until="domcontentloaded", timeout=30000)
                                        _tw.sleep(4)
                                    except:
                                        pass
                                # 表单由 cfCallback 自动提交，等待结果页面落盘
                                _tw.sleep(5)
                            elif "ourbits" in site_url_lower or "我堡" in site_name:
                                # 我堡(ourbits.club)：Cloudflare Turnstile 交互式人机验证
                                # 表单 #attendance 内 <div class="cf-turnstile" data-sitekey=...>，
                                # 通过后 TurnstileCallback 自动提交表单 -> 签到完成
                                import time as _tw
                                try:
                                    _h0 = page.content() or ""
                                    if "已签到" in _h0 or "今天已签到" in _h0:
                                        _log(f"{site_name}: 检测到已签到")
                                        return page.content()
                                except:
                                    pass

                                def _ob_frames():
                                    try:
                                        return [f for f in page.frames
                                                if "challenges.cloudflare.com" in (f.url or "")]
                                    except:
                                        return []

                                def _ob_done():
                                    try:
                                        _h = page.content() or ""
                                        # 注意：我堡 attendance.php 在"未签到"状态也始终带着
                                        # <div class="cf-turnstile">（每次签到都要现场过验证），
                                        # 所以绝不能用"页面含 cf-turnstile"来否决成功——那等于永远失败。
                                        # 真正的判据是签到表单是否消失：提交成功后 #attendance 表单不再渲染。
                                        # 只认明确结果，不再用"表单消失"当判据：
                                        # Playwright 提交瞬间 page.content() 可能抓到中间页/空白页，
                                        # 会误报成功（实测 22:03 就误报过一次）。最终成败交给
                                        # 外层 requests 复核（魔力值增量）决定。
                                        if "签到成功" in _h or "已经签到过了" in _h \
                                                or "您今天已经签到" in _h or "今天已经签到" in _h:
                                            return True
                                        if "次签到" in _h and "获得" in _h and "魔力" in _h:
                                            return True
                                        return False
                                    except:
                                        return False

                                _ob_ok = _ob_done()
                                for _rnd in range(3):
                                    for _i in range(10):
                                        _tw.sleep(2)
                                        if _ob_frames():
                                            break
                                    _sel_used = None
                                    for _f in _ob_frames():
                                        for _sel in ['input[type="checkbox"]', '.ctp-checkbox-label',
                                                     'label', '#challenge-stage', 'body']:
                                            try:
                                                _el = _f.query_selector(_sel)
                                                if _el:
                                                    _el.click(timeout=5000)
                                                    _sel_used = _sel
                                                    break
                                            except:
                                                continue
                                        if _sel_used:
                                            break
                                    _log(f"{site_name}: Turnstile 点击 selector={_sel_used}")
                                    for _i in range(20):
                                        _tw.sleep(3)
                                        if _ob_done():
                                            _log(f"{site_name}: 检测到签到完成（Turnstile通过并提交）")
                                            _ob_ok = True
                                            break
                                    if _ob_ok:
                                        break
                                    try:
                                        page.screenshot(path=f"/tmp/ob_tk_fail_{_rnd + 1}.png")
                                    except:
                                        pass
                                    _log_warn(f"{site_name}: Turnstile 未通过，第{_rnd + 1}次刷新重试")
                                    try:
                                        page.goto(auto_click_url, wait_until="domcontentloaded", timeout=30000)
                                        _tw.sleep(4)
                                    except:
                                        pass
                                _tw.sleep(5)
                            
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

                # 52pt / 自由农场：签到页必须用浏览器执行（滑块验证 + Referer/Cookie 校验），
                # requests 直连会拿到空页/登录页，直接走 Playwright 滑块处理
                slider_browser_site = (
                    ("52pt" in site_url_lower or "52pt" in site_name_lower) or
                    ("0ff" in site_url_lower or "农场" in site_name)
                )
                if slider_browser_site and not auto_click_done:
                    _log(f"{site_name}: 滑块验证站点，直接使用浏览器执行签到...")
                    try:
                        slider_result = self.__handle_site_slider(sign_url, site_name, site_cookie)
                        if slider_result and len(slider_result) > 200:
                            page_source = slider_result
                            auto_click_done = True
                            _log(f"{site_name}: 浏览器滑块处理完成，页面长度={len(page_source)}")
                        else:
                            _log_warn(f"{site_name}: 浏览器滑块处理返回为空")
                    except Exception as e:
                        _log_warn(f"{site_name}: 浏览器滑块处理异常: {e}")

                # 春天/朋友站点：无签到按钮，直接用Playwright访问主页判断登录状态（登录保号）
                no_sign_button_site = (
                    ("springsunday" in site_url_lower or "春天" in site_name) or
                    ("keepfrds" in site_url_lower or "朋友" in site_name)
                )
                if no_sign_button_site and not auto_click_done:
                    _log(f"{site_name}: 无签到按钮站点，优先用requests访问主页判断登录状态...")
                    home_url = f"{site_url.rstrip('/')}/index.php"
                    _ok_requests = False
                    try:
                        import requests as _requests_lib2
                        _h2 = {
                            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                        }
                        if site_cookie:
                            _h2['Cookie'] = site_cookie
                        _r2 = _requests_lib2.get(home_url, headers=_h2, timeout=30, verify=False, proxies=_proxies)
                        _t2 = _r2.content.decode('utf-8', errors='replace') if _r2 is not None else ""
                        if _r2 is not None and _r2.status_code == 200 and _t2 and len(_t2) > 500 and SiteUtils.is_logged_in(_t2):
                            page_source = _t2
                            auto_click_done = True
                            _ok_requests = True
                            _log(f"{site_name}: requests获取主页成功（已登录），长度={len(page_source)}")
                        else:
                            _log_warn(f"{site_name}: requests主页未确认登录，降级Playwright")
                    except Exception as e:
                        _log_warn(f"{site_name}: requests访问主页异常: {e}")
                    if not _ok_requests:
                        try:
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
                http_403 = False
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
                        if resp is not None and resp.status_code == 403:
                            http_403 = True
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
                    # 收紧拦截关键词：仅匹配真正的验证/拦截页，避免命中正常页面里的
                    # 分类链接(cat=403)、站内公告("请耐心等待")、CF Turnstile脚本引用(cloudflare.com)
                    block_keywords = ['雷池', '安全验证', '访问被拒绝', '请求被拦截',
                                      'Just a moment', 'Checking your browser', 'cf-challenge', 'under_challenge',
                                      'Attention Required', 'cf-error', 'you have been blocked']
                    has_403_block = http_403 or any(kw.lower() in page_source.lower() for kw in block_keywords)
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
                # 浏览器滑块处理成功：优先按成功关键词判定，避免后续CF/cloudflare关键词误判
                _browser_success = False
                if auto_click_done and page_source:
                    # 通用页头字段不能当签到信号："魔力值" 出现在每个页面的信息栏
                    # （如 "魔力值 [使用]: 266,354.1"），只要页面能打开就会命中，
                    # 会把"未签到"误判成"签到成功"（我堡 ourbits 曾因此假成功）。
                    # 只认真正的签到结果文案。
                    _strong_kw = ["签到成功", "簽到成功", "签到完成", "已签到", "今日已签到", "已经签到",
                                  "今天已经签过", "已经签过", "今天已经签到", "已經簽到", "今日已簽到",
                                  "请勿重复签到", "請勿重複簽到", "签到已得", "簽到已得",
                                  "已经签到过了", "请勿重复刷新", "您今天已经签到"]
                    _weak_kw = ["获得魔力", "魔力+", "签到奖励", "打卡成功"]
                    if any(_k in page_source for _k in _strong_kw):
                        _browser_success = True
                    elif any(_k in page_source for _k in _weak_kw):
                        # 弱信号：若页面仍挂着 Turnstile 组件，说明人机验证没过，
                        # 页面只是加载出来而已，不能算成功
                        _still_tk = ("cf-turnstile" in page_source
                                     or "TurnstileCallback" in page_source)
                        _browser_success = not _still_tk
                    # 仍有人机验证组件 -> 不算成功。
                    # 例外：我堡每次签到都带 Turnstile 表单，已由上面的魔力值增量单独收口。
                    _tk_keep = ("cf-turnstile" in page_source or "TurnstileCallback" in page_source)
                    _is_ob = ("ourbits" in site_url_lower or "我堡" in site_name)
                    if _browser_success and _tk_keep and not _is_ob:
                        _browser_success = False
                        _log(f"{site_name}: 页面仍含 Turnstile 组件，判定未通过人机验证")

                # 我堡(ourbits)专项收口：Playwright 过完 Turnstile 后，用 requests 复核
                # ①签到表单是否消失 ②魔力值是否增加。两者任一成立即判成功，
                # 彻底摆脱"页面含 cf-turnstile 就失败"的假阴性。
                if ("ourbits" in site_url_lower or "我堡" in site_name) and auto_click_done:
                    try:
                        import requests as _obv, re as _obr3
                        _vs = _obv.Session()
                        _vs.headers.update({
                            "User-Agent": getattr(site, "ua", "") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                            "Accept-Language": "zh-CN,zh;q=0.9",
                            "Cookie": site_cookie or "",
                        })
                        _vr = _vs.get(sign_url, timeout=getattr(site, "timeout", 30) or 30, verify=False)
                        _vt = _vr.content.decode("utf-8", errors="replace")
                        _form_gone = 'id="attendance"' not in _vt
                        _am = _obr3.search(r"\]\s*:\s*([\d,]+(?:\.\d+)?)", _vt)
                        _after_bonus = None
                        if _am:
                            try:
                                _after_bonus = float(_am.group(1).replace(",", ""))
                            except Exception:
                                _after_bonus = None
                        _gain = None
                        if _ob_bonus is not None and _after_bonus is not None:
                            _gain = round(_after_bonus - _ob_bonus, 2)
                        _log(f"{site_name}: 复核 表单消失={_form_gone} 魔力值 {_ob_bonus} -> {_after_bonus} (增量={_gain})")
                        if _form_gone or (_gain is not None and _gain > 0):
                            result = f"✅ {site_name}: 签到成功(魔力值+{_gain if _gain else 0})"
                            _browser_success = True
                        else:
                            # 实测该站服务端会校验 Turnstile token（空 POST 无效、魔力值不变），
                            # 无头/仿真浏览器拿不到有效 token。此时应给出可操作结论，
                            # 不要用"登录成功，签到未确认"这种含糊说法。
                            result = (f"⚠️ {site_name}: 人机验证未通过(Cloudflare Turnstile，"
                                      f"需浏览器手动签到；Cookie 仍有效)")
                            _browser_success = False
                    except Exception as _ve:
                        _log_warn(f"{site_name}: 我堡复核异常 {str(_ve)[:50]}")

                _ob_decided = ("ourbits" in site_url_lower or "我堡" in site_name) and auto_click_done
                if _browser_success:
                    result = f"✅ {site_name}: 签到成功"
                elif _ob_decided:
                    pass  # 我堡已有明确结论（成功或 Turnstile 未通过），不再被通用分支覆盖
                elif page_source and ("audiences" in site_url_lower or "观众" in site_name):
                    # 观众站点：已签到 / Turnstile 未过 / 其他
                    if ("attendance-card--done" in page_source or "今日已签到" in page_source
                            or "已签到，明天再来" in page_source or "签到成功" in page_source):
                        result = f"✅ {site_name}: 已签到"
                    elif "attendance-card--verify" in page_source or "cf-turnstile" in page_source.lower():
                        result = f"⚠️ {site_name}: 人机验证未通过(Cloudflare Turnstile，可稍后重试)"
                    else:
                        result = f"⚠️ {site_name}: 登录成功，签到未确认"
                elif not page_source:
                    _log_warn(f"{site_name}: 请求失败，page_source为空，URL: {sign_url}")
                    # 部分站点（如莫妮卡 monikadesign.uk，Unit3D 架构）没有 attendance.php，
                    # 也就没有签到功能；此时应回退判断首页登录态，登录成功即视为保号成功，
                    # 而不是报"请求失败"这种误导性结论。
                    try:
                        _home = sign_url.rsplit("/", 1)[0] + "/"
                        import requests as _rq
                        _hs = _rq.Session()
                        _hs.headers.update({
                            "User-Agent": getattr(site, "ua", "") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                            "Accept": "text/html,*/*;q=0.8",
                            "Accept-Language": "zh-CN,zh;q=0.9",
                            "Cookie": site_cookie or "",
                        })
                        _hr = _hs.get(_home, timeout=getattr(site, "timeout", 30) or 30, verify=False)
                        _ht = _hr.content.decode("utf-8", errors="replace")
                        if _hr.status_code == 200 and SiteUtils.is_logged_in(_ht):
                            _log(f"{site_name}: 站点无 attendance.php，首页登录正常，按无签到功能保号")
                            result = f"✅ {site_name}: 无签到功能(登录保号)"
                        else:
                            result = f"❌ {site_name}: 请求失败"
                    except Exception as _he:
                        _log_warn(f"{site_name}: 首页回退检测异常 {str(_he)[:40]}")
                        result = f"❌ {site_name}: 请求失败"
                elif under_challenge(page_source):
                    result = f"⚠️ {site_name}: CF挑战(请开启浏览器仿真)"
                elif "Attention Required" in page_source or "cf-error" in page_source or "you have been blocked" in page_source.lower() or "Just a moment" in page_source or "cf-challenge" in page_source.lower():
                    # Cloudflare拦截页面（IP被封或WAF拦截，非标准5秒盾）
                    if site_render:
                        result = f"⚠️ {site_name}: CF拦截(浏览器仿真已开启但IP被封，请浏览器手动过验证后更新Cookie)"
                    else:
                        result = f"⚠️ {site_name}: CF拦截(请开启浏览器仿真或更新Cookie)"
                elif "二级密码" in page_source or "二级验证" in page_source or "请输入二级密码" in page_source or "安全密码" in page_source or "交易密码" in page_source:
                    # 二级密码验证页面（如猪猪等站点，需要手动输入二级密码，无法自动签到）
                    result = f"⚠️ {site_name}: 需要二级密码验证(无法自动签到，请浏览器手动签到)"
                elif "已签到" in page_source or "今日已签到" in page_source or "已经签到" in page_source or "请勿重复签到" in page_source or "今天已签" in page_source or "您今天已经签到" in page_source or "已经签到过了" in page_source or "请勿重复刷新" in page_source or "已簽到" in page_source or "今日已簽到" in page_source or "已經簽到" in page_source or "請勿重複簽到" in page_source or "无需再签" in page_source or "已签到，无需" in page_source or "今日已签到" in page_source:
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
                elif not SiteUtils.is_logged_in(page_source) and not _is_challenge_page(page_source):
                    # Cookie失效（排除反爬/人机验证中间页：那类页面本就不含登录态，
                    # 直接判 Cookie 失效会把雷池/挑战页误报成登录过期，交给后续分支处理）
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
                elif not SiteUtils.is_logged_in(page_source) and not _is_challenge_page(page_source):
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

            import requests as _requests_lib

            def _judge(res):
                """根据响应判定结果，返回 (是否已定论, 结果字符串)"""
                if res is None:
                    return False, ""
                status = res.status_code
                payload = {}
                try:
                    payload = res.json() or {}
                except Exception:
                    payload = {}
                code = payload.get("code")
                msg = str(payload.get("message") or payload.get("msg") or "")
                text_all = (msg + " " + (res.text or "")).lower()
                # 成功
                if status == 200 and code == 0:
                    return True, f"✅ {site_name}: 签到成功"
                # 已签到（各种表达）
                if ("已签到" in msg or "已经签到" in msg or "重复" in msg
                        or "already" in text_all or "今日已签到" in msg):
                    return True, f"✅ {site_name}: 已签到"
                # 200 且无明确 code，视为成功
                if status == 200:
                    return True, f"✅ {site_name}: 签到成功"
                # 401/403 权限
                if status in (401, 403):
                    return True, f"❌ {site_name}: API Key已失效或权限不足"
                return False, ""

            res = None
            # 优先用 api-token header（个人API Key）
            if apikey:
                try:
                    res = _requests_lib.post(
                        url=api_url,
                        headers={**base_headers, "api-token": apikey},
                        json=body, timeout=timeout, verify=False, proxies=proxies)
                except Exception as _e:
                    _log_warn(f"{site_name}: api-token请求异常: {_e}")
                    res = None
                done, out = _judge(res)
                if done:
                    return out

            # 回退 Authorization: Bearer
            if token and (res is None or res.status_code >= 400):
                auth_value = token if token.lower().startswith("bearer ") else f"Bearer {token}"
                try:
                    res2 = _requests_lib.post(
                        url=api_url,
                        headers={**base_headers, "Authorization": auth_value},
                        json=body, timeout=timeout, verify=False, proxies=proxies)
                except Exception as _e:
                    _log_warn(f"{site_name}: Bearer请求异常: {_e}")
                    res2 = None
                if res2 is not None:
                    res = res2
                done, out = _judge(res)
                if done:
                    return out

            # 最终判定
            if res is None:
                return f"❌ {site_name}: 签到失败(无法连接)"
            return f"❌ {site_name}: 签到失败(状态码:{res.status_code})"
        except Exception as e:
            return f"❌ {site_name}: 签到异常: {str(e)[:50]}"


    def __signin_hdarea(self, site, site_name, site_url):
        """高清视界(hdarea.club) JS签到：POST sign_in.php + action=sign_in
        页面首页靠 JS 函数 sign_in('sign_in') 触发，attendance.php 为 404，
        签到结果通过 XHR 返回纯文本：成功="已连续签到N天..." / 重复="请不要重复签到哦！"
        """
        try:
            site_cookie = getattr(site, "cookie", "") or ""
            if not site_cookie:
                return f"❌ {site_name}: 无Cookie"
            ua = getattr(site, "ua", "") or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            timeout = getattr(site, "timeout", 30) or 30
            proxy = getattr(site, "proxy", None)
            import os as _os
            proxies = None
            if proxy:
                proxy_url = _os.environ.get('HTTP_PROXY') or _os.environ.get('HTTPS_PROXY') or 'http://192.168.2.70:7892'
                proxies = {'http': proxy_url, 'https': proxy_url}
            import requests as _req
            base = site_url.rstrip('/')
            headers = {
                "User-Agent": ua,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cookie": site_cookie,
                "Referer": base + "/index.php",
                "X-Requested-With": "XMLHttpRequest",
            }
            resp = _req.post(base + "/sign_in.php", data={"action": "sign_in"},
                             headers=headers, timeout=timeout, verify=False, proxies=proxies)
            if resp is None:
                return f"❌ {site_name}: 签到失败(无响应)"
            body = resp.content.decode('utf-8', errors='replace') or ""
            body_s = body.strip()
            if "请不要重复签到" in body_s or "已签到" in body_s:
                return f"✅ {site_name}: 已签到"
            if "签到成功" in body_s or "连续签到" in body_s or "获得" in body_s or "奖励" in body_s:
                return f"✅ {site_name}: 签到成功"
            if not body_s:
                # 空响应也视为已成功提交（站点重复签到偶发空返回）
                return f"✅ {site_name}: 已签到"
            return f"⚠️ {site_name}: 签到未确认({body_s[:40]})"
        except Exception as e:
            return f"❌ {site_name}: 签到异常: {str(e)[:50]}"


    def __signin_kelu(self, site, site_name, site_url):
        """Kelu(our.kelu.one) NexusPHP 图形验证码签到：用 ddddocr 识别 imagestring 后 POST 提交"""
        try:
            site_cookie = getattr(site, "cookie", "") or ""
            if not site_cookie:
                return f"❌ {site_name}: 无Cookie"
            ua = getattr(site, "ua", "") or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
            timeout = getattr(site, "timeout", 30) or 30
            proxy = getattr(site, "proxy", None)

            import os as _os
            proxies = None
            if proxy:
                proxy_url = _os.environ.get('HTTP_PROXY') or _os.environ.get('HTTPS_PROXY') or 'http://192.168.2.70:7892'
                proxies = {'http': proxy_url, 'https': proxy_url}

            import requests as _req
            base = site_url.rstrip('/')
            attendance_url = base + "/attendance.php"
            headers = {
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cookie": site_cookie,
            }

            # OCR 引擎（懒加载，缺失则降级）
            try:
                _ocr = _load_ocr()
            except Exception as _e:
                return f"⚠️ {site_name}: 需要图形验证码(OCR组件缺失: {str(_e)[:40]})"

            for attempt in range(3):
                sess = _req.Session()
                r = sess.get(attendance_url, headers=headers, timeout=timeout, verify=False, proxies=proxies)
                page = r.content.decode('utf-8', errors='replace') if r else ""

                # 已签到
                if any(k in page for k in ["签到成功", "今日已签到", "已经签到", "已签到"]):
                    return f"✅ {site_name}: 已签到"
                # 登录失效
                if not page or "takelogin.php" in page or "登录" in page[:2000] and "imagestring" not in page:
                    if "imagestring" not in page and ("登录" in page or "login" in page.lower()):
                        return f"❌ {site_name}: Cookie失效"

                m_hash = re.search(r'name="imagehash"\s+value="([^"]+)"', page)
                m_img = re.search(r'<img[^>]*src="(image\.php[^"]+)"', page, re.I)
                if not m_hash or not m_img:
                    # 没有验证码表单，页面又无明确成功标记 -> 未确认
                    if "imagestring" in page:
                        pass
                    else:
                        return f"⚠️ {site_name}: 登录成功，签到未确认"

                imagehash = m_hash.group(1)
                img_url = base + "/" + m_img.group(1).replace("&amp;", "&")
                try:
                    ri = sess.get(img_url, headers={**headers, "Referer": attendance_url},
                                  timeout=timeout, verify=False, proxies=proxies)
                    code = _ocr.classification(ri.content) or ""
                except Exception as _e:
                    _log_warn(f"{site_name}: 验证码识别异常: {_e}")
                    continue
                code = re.sub(r"[^0-9A-Za-z]", "", str(code)).strip()
                if not code:
                    continue
                _log(f"{site_name}: 验证码识别={code} (第{attempt + 1}次)")

                rp = sess.post(attendance_url,
                               data={"imagestring": code, "imagehash": imagehash, "submit": "立即签到"},
                               headers={**headers, "Referer": attendance_url,
                                        "Content-Type": "application/x-www-form-urlencoded"},
                               timeout=timeout, verify=False, allow_redirects=True, proxies=proxies)
                tp = rp.content.decode('utf-8', errors='replace') if rp else ""
                if "签到成功" in tp or "获得" in tp or "魔力" in tp and "验证码" not in tp[:4000]:
                    if "签到成功" in tp:
                        return f"✅ {site_name}: 签到成功"
                if any(k in tp for k in ["今天已经签过", "已经签过", "今日已签到", "已经签到"]):
                    return f"✅ {site_name}: 已签到"
                if "验证码" in tp and ("错误" in tp or "不正确" in tp or "invalid" in tp.lower()):
                    _log_warn(f"{site_name}: 验证码错误，重试")
                    continue
                # 其它情况：若已无验证码表单，视为成功
                if "imagestring" not in tp and "签到" in tp:
                    return f"✅ {site_name}: 签到成功"

            return f"⚠️ {site_name}: 登录成功，签到未确认"
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

    def __signin_agsvpt(self, site, site_name, site_url):
        """AGSVPT(agsvpt.com) NexusPHP 签到：需 POST attendance.php 才真正签到"""
        try:
            import requests as _requests_lib
            site_cookie = getattr(site, "cookie", "") or ""
            if not site_cookie:
                return f"❌ {site_name}: 无Cookie"
            ua = getattr(site, "ua", "") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            timeout = getattr(site, "timeout", 30) or 30

            s = _requests_lib.Session()
            s.headers.update({
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": site_cookie,
            })
            url = f"{site_url.rstrip('/')}/attendance.php"
            # 先 GET 签到页（初始化会话）
            s.get(url, timeout=timeout, verify=False)
            # 再 POST 触发签到
            s.headers["Referer"] = url
            s.headers["Content-Type"] = "application/x-www-form-urlencoded"
            res = s.post(url, data={}, timeout=timeout, verify=False, allow_redirects=True)
            if not res or res.status_code != 200:
                return f"❌ {site_name}: 签到失败(状态码:{res.status_code if res else '无响应'})"
            text = res.content.decode("utf-8", errors="replace")
            if "签到成功" in text:
                # 提取连续签到天数
                import re as _re
                m = _re.search(r"第\s*<b>\s*(\d+)\s*</b>\s*次签到", text)
                days = m.group(1) if m else ""
                return f"✅ {site_name}: 签到成功" + (f"(第{days}次)" if days else "")
            if "已签到" in text or "无需" in text:
                return f"✅ {site_name}: 已签到"
            if "验证码" in text or "captcha" in text.lower():
                return f"⚠️ {site_name}: 需要图形验证码"
            return f"⚠️ {site_name}: 签到未确认"
        except Exception as e:
            return f"❌ {site_name}: 签到异常: {str(e)[:50]}"

    def __signin_hdsky(self, site, site_name, site_url):
        """天空(hdsky.me) 签到真实流程：
        1) POST showup.php {action:check}  ->  {sign:true} 表示已签到
        2) POST image_code_ajax.php {action:new}  ->  {code:<imagehash>}
        3) GET  image.php?action=regimage&imagehash=<code>  ->  验证码图，用 ddddocr 识别
        4) POST showup.php {action:showup,imagehash,imagestring}  ->  {success:true,message:<魔力>}
        """
        try:
            import requests as _req
            site_cookie = getattr(site, "cookie", "") or ""
            if not site_cookie:
                return f"❌ {site_name}: 无Cookie"
            ua = getattr(site, "ua", "") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            timeout = getattr(site, "timeout", 30) or 30
            base = site_url.rstrip('/')

            s = _req.Session()
            s.headers.update({
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": site_cookie,
                "Referer": base + "/index.php",
            })

            # 1) 是否已签到
            r = s.post(base + "/showup.php", data={"action": "check"}, timeout=timeout, verify=False)
            try:
                d = r.json()
            except Exception:
                d = {}
            if d.get("sign") is True:
                return f"✅ {site_name}: 已签到"

            # OCR 引擎
            try:
                _ocr = _load_ocr()
            except Exception as _e:
                return f"⚠️ {site_name}: 需要图形验证码(OCR组件缺失: {str(_e)[:40]})"

            for _attempt in range(3):
                # 2) 取新验证码 code
                r2 = s.post(base + "/image_code_ajax.php", data={"action": "new"},
                            timeout=timeout, verify=False)
                code = ""
                try:
                    code = (r2.json() or {}).get("code") or ""
                except Exception:
                    import re as _re2
                    m = _re2.search(r'"code"\s*:\s*"([0-9a-fA-F]+)"', r2.text or "")
                    code = m.group(1) if m else ""
                if not code:
                    continue
                # 3) 取验证码图并 OCR
                img = s.get(base + "/image.php?action=regimage&imagehash=" + code,
                            timeout=timeout, verify=False)
                if not img or not img.content:
                    continue
                try:
                    txt = _ocr.classification(img.content)
                except Exception:
                    continue
                txt = (txt or "").strip()
                if not txt:
                    continue
                # 4) 提交
                r4 = s.post(base + "/showup.php",
                            data={"action": "showup", "imagehash": code, "imagestring": txt},
                            timeout=timeout, verify=False)
                try:
                    d4 = r4.json()
                except Exception:
                    d4 = {}
                if d4.get("success") is True:
                    msg = d4.get("message")
                    return f"✅ {site_name}: 签到成功(获得{msg}魔力值)"
                if str(d4.get("message")) == "invalid_imagehash":
                    continue  # 验证码识别错误，重试
                # 其它情况再试一次
            return f"⚠️ {site_name}: 签到未确认(验证码识别失败)"
        except Exception as e:
            return f"❌ {site_name}: 签到异常: {str(e)[:50]}"

    def __signin_hhanclub(self, site, site_name, site_url):
        """憨憨(hhanclub.net) 签到：站点每日自动签到，日历 data 内嵌当日记录。
        判定：解析 attendance.php 内嵌 data JSON，若含今日日期即为已签到。
        若当日缺失，则尝试 POST attendance.php 触发签到。
        """
        try:
            import requests as _req
            import re as _re
            from datetime import datetime as _dt
            site_cookie = getattr(site, "cookie", "") or ""
            if not site_cookie:
                return f"❌ {site_name}: 无Cookie"
            ua = getattr(site, "ua", "") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            timeout = getattr(site, "timeout", 30) or 30
            base = site_url.rstrip('/')

            s = _req.Session()
            s.headers.update({
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": site_cookie,
            })

            def _today_in_data(html):
                try:
                    td = _dt.now().strftime("%Y-%m-%d")
                    return ('"' + td + '":{') in html or ("'" + td + "':") in html
                except Exception:
                    return False

            att = s.get(base + "/attendance.php", timeout=timeout, verify=False)
            html = att.content.decode("utf-8", errors="replace")
            if not html or len(html) < 500:
                return f"❌ {site_name}: 请求失败"
            if _today_in_data(html) or "今天已签到" in html or "今日已签到" in html:
                return f"✅ {site_name}: 已签到"
            # 未签到：尝试 POST 触发签到，随后复查
            try:
                s.post(base + "/attendance.php", data={"action": "attendance"},
                       timeout=timeout, verify=False,
                       headers={"Referer": base + "/attendance.php"})
            except Exception:
                pass
            att2 = s.get(base + "/attendance.php", timeout=timeout, verify=False)
            html2 = att2.content.decode("utf-8", errors="replace")
            if _today_in_data(html2):
                return f"✅ {site_name}: 签到成功"
            return f"⚠️ {site_name}: 登录成功，签到未确认"
        except Exception as e:
            return f"❌ {site_name}: 签到异常: {str(e)[:50]}"


    def __signin_opencd(self, site, site_name, site_url):
        """皇后(open.cd) 签到真实流程：
        1) GET  plugin_sign-in.php  -> 取 imagehash + 验证码图（6位字母数字）
        2) ddddocr 识别验证码
        3) POST plugin_sign-in.php?cmd=signin  (imagehash, imagestring)
           -> {"state":"success","signindays":..,"integral":..}
        """
        try:
            import requests as _req
            import re as _re
            site_cookie = getattr(site, "cookie", "") or ""
            if not site_cookie:
                return f"❌ {site_name}: 无Cookie"
            ua = getattr(site, "ua", "") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            timeout = getattr(site, "timeout", 30) or 30
            base = site_url.rstrip('/')

            s = _req.Session()
            s.headers.update({
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": site_cookie,
            })

            # 首页检查已签到
            home = s.get(base + "/index.php", timeout=timeout, verify=False)
            ht = home.content.decode("utf-8", errors="replace")
            if "已簽到" in ht or "已签到" in ht:
                return f"✅ {site_name}: 已签到"

            # 签到前魔力值基线（签到成功 +10），用于结果兜底判定
            import re as _re_bonus
            _b0 = None
            _bm0 = _re_bonus.search(r"魔力值\s*:\s*([\d,\.]+)", ht)
            if _bm0:
                try:
                    _b0 = float(_bm0.group(1).replace(",", ""))
                except Exception:
                    _b0 = None

            try:
                _ocr = _load_ocr()
            except Exception as _e:
                return f"⚠️ {site_name}: 需要图形验证码(OCR组件缺失: {str(_e)[:40]})"

            sign_page = base + "/plugin_sign-in.php"
            for _attempt in range(6):
                sp = s.get(sign_page, timeout=timeout, verify=False)
                spt = sp.content.decode("utf-8", errors="replace")
                if "已簽到" in spt or "已签到" in spt:
                    return f"✅ {site_name}: 已签到"
                mh = _re.search(r'name="imagehash"\s+value="([^"]+)"', spt)
                mi = _re.search(r'src="(image\.php\?action=regimage[^"]+)"', spt)
                if not (mh and mi):
                    return f"⚠️ {site_name}: 签到页结构变化"
                img_url = base + "/" + mi.group(1).replace("&amp;", "&")
                img = s.get(img_url, timeout=timeout, verify=False)
                if not img or not img.content:
                    continue
                txt = (_ocr.classification(img.content) or "").strip()
                # 皇后验证码固定 6 位字母数字，识别结果长度不符直接换一张，别浪费提交次数
                if len(txt) != 6 or not _re.match(r"^[A-Za-z0-9]{6}$", txt):
                    _log_warn(f"{site_name}: 验证码识别结果异常('{txt}' {len(txt)}位)，换一张重试")
                    continue
                r2 = s.post(base + "/plugin_sign-in.php?cmd=signin",
                            data={"imagehash": mh.group(1), "imagestring": txt},
                            timeout=timeout, verify=False)
                body = r2.content.decode("utf-8", errors="replace")
                try:
                    d2 = r2.json()
                except Exception:
                    d2 = {}
                if d2.get("state") == "success":
                    return f"✅ {site_name}: 签到成功(获得{d2.get('integral')}积分)"
                # state:false 且无 msg = 今日已签过（区别于 msg:"验证码错误"）
                if d2.get("state") == "false" and not d2.get("msg"):
                    return f"✅ {site_name}: 已签到(今日已签)"
                if "验证码" in body or "錯誤" in body or "错误" in body:
                    continue  # 验证码识别错误，重试
                # 其它未知响应：可能已签（服务端静默 +10），用魔力值增量做最终判定
                try:
                    _hp = s.get(base + "/index.php", timeout=timeout, verify=False)
                    _hpt = _hp.content.decode("utf-8", errors="replace")
                    _bm1 = _re_bonus.search(r"魔力值\s*:\s*([\d,\.]+)", _hpt)
                    if _bm1 and _b0 is not None:
                        _b1 = float(_bm1.group(1).replace(",", ""))
                        if _b1 > _b0:
                            return f"✅ {site_name}: 签到成功(魔力值+{round(_b1 - _b0, 1)})"
                except Exception:
                    pass
                continue
            # 6 次都失败：最后一次确认今日是否已签（服务端对重复签到返回 state:false 无 msg）
            try:
                _hp = s.get(base + "/index.php", timeout=timeout, verify=False)
                _hpt = _hp.content.decode("utf-8", errors="replace")
                _bm1 = _re_bonus.search(r"魔力值\s*:\s*([\d,\.]+)", _hpt)
                if _bm1 and _b0 is not None:
                    _b1 = float(_bm1.group(1).replace(",", ""))
                    if _b1 > _b0:
                        return f"✅ {site_name}: 签到成功(魔力值+{round(_b1 - _b0, 1)})"
            except Exception:
                pass
            return f"⚠️ {site_name}: 签到未确认(验证码识别失败)"
        except Exception as e:
            return f"❌ {site_name}: 签到异常: {str(e)[:50]}"

    def __signin_pttime(self, site, site_name, site_url):
        """PT时间(pttime.org) 签到。

        签到端点是 attendance.php，返回的是**纯文本**而非 HTML 页面：
            "签到成功"                    -> 本次签到成功
            "拒绝访问：已签到，无需再签"    -> 今日已签（算成功）
            "err cookie"                  -> Cookie 失效

        历史 bug（v3.9.10 及以前一直误报成功）：
        1) URL 映射写成了 attendance.php?type=list —— 那是"签到记录列表页"，
           请求它不会签到，页面里也没有签到结果；
        2) 通用判定调用了 SiteUtils.is_checkin()，其 XPath 含
           //a[contains(@href, "attendance")]，而首页导航恒有
           <a href="attendance.php?type=list">签到详情</a>，
           于是无论如何都返回"已签到" —— 永久假阳性。
        因此该站必须独立判定：只认签到端点返回的纯文本。
        """
        try:
            import requests as _req
            import re as _re
            site_cookie = getattr(site, "cookie", "") or ""
            if not site_cookie:
                return f"❌ {site_name}: 无Cookie"
            ua = getattr(site, "ua", "") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            timeout = getattr(site, "timeout", 30) or 30
            base = site_url.rstrip("/")

            s = _req.Session()
            s.headers.update({
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": site_cookie,
                "Referer": base + "/index.php",
            })

            def _bonus(html):
                # 页头结构：<a href="mybonus.php">使用&说明</a>]：553510.1
                m = _re.search(r"\]\s*[：:]\s*([\d,]+(?:\.\d+)?)", html)
                return m.group(1) if m else None

            _b0 = None
            try:
                _h0 = s.get(base + "/index.php", timeout=timeout, verify=False)
                _b0 = _bonus(_h0.content.decode("utf-8", errors="replace"))
            except Exception:
                pass

            last = ""
            for _attempt in range(3):
                r = s.get(base + "/attendance.php", timeout=timeout, verify=False)
                body = (r.content.decode("utf-8", errors="replace") or "").strip()
                last = body
                low = body.lower()
                if "err cookie" in low:
                    return f"❌ {site_name}: Cookie失效"
                # 极短纯文本响应，直接按文案判定，不套 HTML 关键词匹配
                if "签到成功" in body:
                    return f"✅ {site_name}: 签到成功"
                if "已签到" in body or "无需再签" in body:
                    return f"✅ {site_name}: 已签到(今日已签)"
                import time as _t
                _t.sleep(3)

            # 三次未知响应：用魔力值增量做最终判定
            try:
                _h1 = s.get(base + "/index.php", timeout=timeout, verify=False)
                _b1 = _bonus(_h1.content.decode("utf-8", errors="replace"))
                if _b0 and _b1:
                    _a = float(str(_b0).replace(",", ""))
                    _c = float(str(_b1).replace(",", ""))
                    if _c > _a:
                        return f"✅ {site_name}: 签到成功(魔力值+{round(_c - _a, 1)})"
            except Exception:
                pass
            _log_warn(f"{site_name}: 签到响应异常 -> {last[:80]}")
            return f"⚠️ {site_name}: 签到未确认"
        except Exception as e:
            return f"❌ {site_name}: 签到异常: {str(e)[:50]}"

    def __signin_ptchdbits(self, site, site_name, site_url):
        """彩虹岛(ptchdbits.co) 签到：bakatest.php 每日答题签到。

        站点机制（2026-10-18 实测确认）：
        - 入口恒为 bakatest.php，页面顶部有权威状态行：
              "今天已经签过到了(已连续N天签到)"   -> 今日已签
        - 未签到时页面给出题目表单：questionid + choice[]（单选/多选）+ 提交
        - 提交后服务端重新出题（可无限答题），因此**不能**用"页面有没有题目"
          判断签到成败，必须回读顶部状态行。
        - wantskip=不会 单独提交会返回"没有选择选项哦"，无效。

        历史 bug（<= v3.9.11，长期假成功）：
        判定写的是 if "回答正确" in t3 ...，而该站页面底部规则区**固定**包含
        "不会却回答正确额外获得5魔力值"这段文案，导致该条件无条件成立，
        无论答对答错都返回"✅ 签到成功"。
        修复：删除所有静态文案判定，改为回读"今天已经签过到"状态行；
        辅以魔力值增量兜底。
        """
        try:
            import requests as _req
            import re as _re
            import time as _tw

            site_cookie = getattr(site, "cookie", "") or ""
            if not site_cookie:
                return f"❌ {site_name}: 无Cookie"
            ua = getattr(site, "ua", "") or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                                             "Chrome/120.0.0.0 Safari/537.36")
            timeout = getattr(site, "timeout", 30) or 30
            base = site_url.rstrip("/")
            page_url = base + "/bakatest.php"

            s = _req.Session()
            s.headers.update({
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Cookie": site_cookie,
                "Referer": base + "/index.php",
            })

            def _norm(t):
                return (t or "").replace("&nbsp;", " ")

            def _signed(t):
                """回读站点自己的状态行，这是唯一权威判据"""
                t = _norm(t)
                return ("今天已经签过到" in t or "已经签过到" in t
                        or "今天已經簽到" in t or "已签到" in t)

            def _streak(t):
                m = _re.search(r"已连续\s*(\d+)\s*天", _norm(t))
                return m.group(1) if m else None

            def _magic(t):
                # 页头原文是：魔力值 </font>[<a href="mybonus.php">使用</a>]: 1,382,415.3
                # 「魔力值」与「使用」之间夹着 </font> 和 <a>，直接把标签剥掉再匹配才可靠。
                t = _norm(t)
                t = _re.sub(r"<script.*?</script>", " ", t, flags=_re.S)
                t = _re.sub(r"<style.*?</style>", " ", t, flags=_re.S)
                t = _re.sub(r"<[^>]+>", " ", t)
                t = " ".join(t.split())
                m = _re.search(r"魔力值\s*\[\s*使用\s*\]\s*[:：]\s*([\d,]+(?:\.\d+)?)", t)
                if not m:
                    m = _re.search(r"魔力值\s*[:：]\s*([\d,]+(?:\.\d+)?)", t)
                if not m:
                    return None
                try:
                    return float(m.group(1).replace(",", ""))
                except Exception:
                    return None

            def _fetch():
                r = s.get(page_url, timeout=timeout, verify=False)
                return r.status_code, _norm(r.content.decode("utf-8", errors="replace"))

            st, page = _fetch()
            if st != 200:
                return f"❌ {site_name}: 无法访问签到页(状态码:{st})"
            if "err cookie" in page.lower() or "你需要先登录" in page:
                return f"❌ {site_name}: Cookie失效"

            # 开场先判今日是否已签
            if _signed(page):
                _n = _streak(page)
                # 该站奖励公式：随机取得(连续天数)~(连续天数*2)魔力值。
                # 连续天数为 0 时奖励区间是 0~0，即本次签到奖励为 0，
                # 这不是失败，站点状态行已明确"今天已经签过到了"。
                if _n == "0":
                    return f"✅ {site_name}: 已签到(连续0天，按站点公式本次奖励为0魔力)"
                return f"✅ {site_name}: 已签到" + (f"(连续{_n}天)" if _n else "")

            m0 = _magic(page)
            # 该站「今天已经签过到了」与答题表单会同时出现（答完还给新题可无限练习），
            # 既然站点自己说今天已签，就不要再提交答案——多答多扣分。
            qid = _re.search(r'name="questionid"\s+value="(\d+)"', page)
            opts = _re.findall(r"name='choice\[\]'\s+value='(\d+)'\s*>\s*([^<]+)", page)
            if not (qid and opts):
                return f"⚠️ {site_name}: 无题目表单且未见已签标记(页面结构变化)"

            qtext = ""
            mq = _re.search(r"\[([^\]]*)\]\s*\[([^\]]*)\]\s*(?:请问：)?([^<\n]+)", page)
            if mq:
                qtext = mq.group(3).strip()
            multi = "多选" in page or "多選" in page

            opt_map = {txt.strip(): v for v, txt in opts}

            # ---- 题库：题干特征词 -> 正确选项文本 ----
            _BANK = {
                "嘻哈三部曲": ["绝世高手", "婚纱照", "浮游目击者"],
                "大业炎": ["弃掉四张不同花色手牌"],
                "第一次世界大战的战败国": ["德国", "奥匈帝国", "奥斯曼帝国", "保加利亚"],
            }
            chosen = []
            for k, ans in _BANK.items():
                if k in qtext:
                    chosen = [opt_map[a] for a in ans if a in opt_map]
                    break

            # 天干地支类
            if not chosen:
                tiangan = "甲乙丙丁戊己庚辛壬癸"
                dizhi = "子丑寅卯辰巳午未申酉戌亥"
                ch = None
                if "天干" in qtext:
                    ch = tiangan[-1] if ("最后" in qtext or "末" in qtext) else tiangan[0]
                elif "地支" in qtext:
                    ch = dizhi[-1] if ("最后" in qtext or "末" in qtext) else dizhi[0]
                if ch:
                    for v, txt in opts:
                        if txt.strip().startswith(ch):
                            chosen = [v]
                            break

            if not chosen:
                # 未收录：**绝不盲答**。
                # 该站规则第 5 条：回答错误扣 1 个魔力（换题扣更多）。
                # 实测盲选 4 项后魔力值 1,382,414.3 -> 1,382,413.3（-1.0）。
                # 盲答只会亏魔力，且站点顶部状态行已说明今日签到状态，
                # 因此未命中题库时直接放弃作答，按状态行如实汇报。
                _log_warn(f"{site_name}: 题库未收录，为避免答错扣魔力，放弃作答: {qtext[:40]}")
                _n = _streak(page)
                return (f"⚠️ {site_name}: 未收录题目已跳过(避免答错扣魔力)"
                        + (f"，今日已签(连续{_n}天)" if _signed(page) else ""))

            data = [("questionid", qid.group(1)),
                    ("usercomment", "此刻心情:无"),
                    ("submit", "提交")]
            for v in chosen:
                data.append(("choice[]", v))
            s.post(page_url, data=data, timeout=timeout, verify=False)
            _tw.sleep(2)

            # 回读状态行做二次验证（首要判据）
            _tw.sleep(1)
            st2, page2 = _fetch()
            if _signed(page2):
                n = _streak(page2)
                m1 = _magic(page2)
                if m0 is not None and m1 is not None and m1 > m0:
                    return f"✅ {site_name}: 签到成功(魔力值+{round(m1 - m0, 1)})"
                if n == "0":
                    return f"✅ {site_name}: 签到成功(连续0天，按站点公式本次奖励为0魔力)"
                return f"✅ {site_name}: 签到成功" + (f"(连续{n}天)" if n else "")

            # 兜底：魔力值增量（做种也会涨，仅作参考）
            m1 = _magic(page2)
            if m0 is not None and m1 is not None and m1 > m0:
                return f"✅ {site_name}: 签到成功(魔力值+{round(m1 - m0, 1)})"

            return f"⚠️ {site_name}: 答题已提交但未确认签到(请浏览器核对)"
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
            is_farm = '0ff' in sign_url.lower() or '农场' in site_name
            
            if is_52pt:
                return self.__handle_52pt_slider(page, site_name)
            
            if is_farm:
                return self.__handle_farm_slider(page, site_name, sign_url, site_cookie)
            
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
        # 52PT：起手页必须是首页（先加载签到页会被站点判定为异常并返回"暂停"页）
        _start_url = sign_url
        if '52pt' in site_name.lower() or '52pt' in (sign_url or '').lower():
            _start_url = 'https://52pt.site/52bakatestdate0823.php'
        try:
            _slider_timeout = 120 if ('52pt' in site_name.lower() or '52pt' in (sign_url or '').lower()) else 60
            result = PlaywrightHelper().action(
                url=_start_url,
                callback=slider_callback,
                cookies=site_cookie,
                ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                headless=True,
                timeout=_slider_timeout
            )
            return result
        except Exception as e:
            _log_warn(f"{site_name}: PlaywrightHelper滑块处理失败: {e}")
            return None
    
    def __handle_52pt_slider(self, page, site_name):
        """52PT专门处理：签到页校验 Referer 且滑块校验拖动轨迹。
        流程：带 Referer 打开签到页 -> 已签到则直接返回 -> 有滑块则真实鼠标拖动并提交。
        """
        import time as _t

        SIGN_PAGE = 'https://52pt.site/52bakatestdate0823.php'
        HOME = 'https://52pt.site/index.php'

        def _page_text():
            try:
                return page.inner_text('body') or ''
            except Exception:
                return ''

        def _has_slider():
            try:
                return page.query_selector('#slider-btn') is not None
            except Exception:
                return False

        try:
            # 第1步：带 Referer 直接打开签到页
            _opened = False
            try:
                page.goto(SIGN_PAGE, wait_until='domcontentloaded', timeout=25000, referer=HOME)
                _t.sleep(1.2)
                _opened = True
            except Exception as e:
                _log_warn(f"{site_name}: 打开签到页异常: {e}")

            # 第2步：已签到判定（签到后页面无滑块，显示"今天已经签过到了"）
            if _opened:
                txt = _page_text()
                if any(k in txt for k in ['今天已经签过', '已经签过', '今天已经签到', '已签到', '今日已签']):
                    _log(f"{site_name}: 检测到今日已签到")
                    return page.content()

            # 第3步：无滑块 -> 尝试从主页点击签到链接（兼容旧版 a#game 入口）
            if _opened and not _has_slider():
                _log(f"{site_name}: 签到页无滑块，尝试从主页进入")
                try:
                    page.goto(HOME, wait_until='domcontentloaded', timeout=20000)
                    _t.sleep(0.8)
                    sign_link = page.query_selector('a#game')
                    if sign_link is None:
                        # 兜底：按文本找"签到赚魔力"
                        try:
                            sign_link = page.query_selector('a:has-text("签到赚魔力")')
                        except Exception:
                            sign_link = None
                    if sign_link:
                        try:
                            sign_link.click()
                            try:
                                page.wait_for_url('**/52bakatestdate0823.php', timeout=10000)
                            except Exception:
                                pass
                            _t.sleep(2)
                            _log(f"{site_name}: 已从主页进入签到页")
                        except Exception as e:
                            _log_warn(f"{site_name}: 点击签到链接失败: {e}")
                except Exception as e:
                    _log_warn(f"{site_name}: 主页导航异常: {e}")

                # 再次判定
                txt = _page_text()
                if any(k in txt for k in ['今天已经签过', '已经签过', '今天已经签到', '已签到', '今日已签']):
                    _log(f"{site_name}: 检测到今日已签到")
                    return page.content()

            # 第4步：拖滑块
            try:
                page.wait_for_selector('#slider-btn', timeout=12000)
            except Exception:
                pass
            _t.sleep(0.5)
            try:
                _btn = page.query_selector('#slider-btn')
                _con = page.query_selector('#slider-container')
                if not _btn or not _con:
                    _log_warn(f"{site_name}: 52PT滑块元素未找到")
                    return page.content()
                _bb = _btn.bounding_box()
                _cb = _con.bounding_box()
                if not _bb or not _cb:
                    _log_warn(f"{site_name}: 52PT滑块位置获取失败")
                    return page.content()
                _sx = _bb['x'] + _bb['width'] / 2
                _sy = _bb['y'] + _bb['height'] / 2
                _ex = _cb['x'] + _cb['width'] - 22
                _mouse = page.mouse
                _mouse.move(_sx, _sy)
                _t.sleep(0.2)
                _mouse.down()
                _t.sleep(0.15)
                _steps = 25
                for _i in range(1, _steps + 1):
                    _mouse.move(_sx + (_ex - _sx) * _i / _steps, _sy)
                    _t.sleep(0.02)
                _mouse.up()
                _t.sleep(1.5)
                _cap = ''
                try:
                    _cap = page.eval_on_selector('#sign_captcha', 'el => el.value')
                except Exception:
                    pass
                _log(f"{site_name}: 52PT滑块拖动完成，sign_captcha={_cap}")
            except Exception as e:
                _log_warn(f"{site_name}: 52PT滑块拖动异常: {e}")
            _t.sleep(0.5)

            # 第5步：提交
            submit_btn = page.query_selector('#submit-btn')
            if submit_btn:
                is_enabled = submit_btn.is_enabled()
                _log(f"{site_name}: 52PT提交按钮状态: enabled={is_enabled}")
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
            _log_warn(f"{site_name}: 52PT处理异常: {e}")
            return page.content()

    def __handle_farm_slider(self, page, site_name, sign_url=None, site_cookie=None):
        """农场滑块专门处理：用真实鼠标拖动完成验证（农场有轨迹检测，JS直接设置位置不生效）"""
        import time as _t
        import random as _r2

        # 关键修复：把 Cookie 写入浏览器 CookieJar 并清除强制请求头。
        # PlaywrightHelper 只通过 extra_http_headers 注入 cookie，不写 CookieJar；
        # 滑块验证后页面会 reload，此时站点读不到浏览器 cookie 会被判为"未登录"，
        # 导致拖完滑块仍返回登录页。
        try:
            if site_cookie:
                from urllib.parse import urlparse
                _domain = urlparse(sign_url or 'https://pt.0ff.cc').netloc or 'pt.0ff.cc'
                _cl = []
                for _part in site_cookie.split(';'):
                    if '=' in _part:
                        _k, _v = _part.strip().split('=', 1)
                        _cl.append({'name': _k, 'value': _v, 'domain': _domain, 'path': '/'})
                if _cl:
                    page.context.add_cookies(_cl)
                    page.set_extra_http_headers({})
                    _log(f"{site_name}: 已将Cookie同步到浏览器并清除强制请求头")
                    # 重新加载，让 CookieJar 生效（extra header 已清除）
                    try:
                        page.goto(sign_url or 'https://pt.0ff.cc/attendance.php',
                                  wait_until='domcontentloaded', timeout=30000)
                    except Exception:
                        pass
        except Exception as _e:
            _log_warn(f"{site_name}: 同步Cookie到浏览器失败: {_e}")

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
                cur_y = start_y + (_r2.random() - 0.5) * 4
                mouse.move(cur_x, cur_y)
                _t.sleep(0.02 + _r2.random() * 0.03)
            
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
            rest = result[1:].strip().lstrip("\ufe0f\u200d")  # 清理 emoji 变体选择符
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
        """签到主入口：单线程顺序签到，站点间随机间隔，失败自动重试一次"""
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

        total = len(sign_sites)
        _log(f"开始签到，共{total}个站点，单线程顺序执行")

        results_map = {}
        failed_sites = []

        # 第一轮：单线程顺序签到，站点间随机间隔避免触发风控/CF
        for idx, site in enumerate(sign_sites, 1):
            try:
                _log(f"[{idx}/{total}] {site.name}: 开始签到...")
                result = self.__sign_one_site(site)
            except Exception as e:
                _log_error(f"{site.name}: 签到异常 {e}")
                result = f"❌ {site.name}: 异常 {str(e)[:50]}"
            results_map[site.name] = result
            if result.startswith("❌"):
                failed_sites.append(site)
            _log(f"[{idx}/{total}] {result}")
            if idx < total:
                time.sleep(random.uniform(2.0, 5.0))

        # 第二轮：失败站点等待后重试一次（CF/网络抖动常可在重试时通过）
        if failed_sites:
            _log(f"第一轮 {len(failed_sites)} 个站点失败，等待15秒后重试...")
            time.sleep(15)
            for ridx, site in enumerate(failed_sites, 1):
                try:
                    _log(f"重试[{ridx}/{len(failed_sites)}] {site.name}...")
                    retry_result = self.__sign_one_site(site)
                    if retry_result.startswith("✅") or retry_result.startswith("⚠️"):
                        results_map[site.name] = retry_result
                        _log(f"重试结果: {retry_result}")
                    else:
                        _log_warn(f"重试仍失败: {retry_result}")
                except Exception as e:
                    _log_error(f"{site.name}: 重试异常 {e}")
                time.sleep(random.uniform(3.0, 6.0))

        # 按站点名称排序输出
        results = sorted(results_map.values())

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