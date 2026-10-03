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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta as _td
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger


class ProSignin(_PluginBase):
    """站点自动签到Pro。"""

    plugin_name = "站点自动签到Pro"
    plugin_desc = "自动签到所有已选站点，并发队列+失败重试+智能降级+详细数据统计页(今日状态+7天历史)，支持清理缓存。"
    plugin_icon = "https://img.icons8.com/fluency/96/calendar.png"
    plugin_version = "3.0.0"
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
        """详细数据页面：统计卡片+签到状态表格+7天历史"""
        history = self.__load_history()
        today = datetime.now().strftime("%Y-%m-%d")
        today_data = history.get("history", {}).get(today, {})

        # 统计
        total = len(today_data)
        success = sum(1 for v in today_data.values() if v.get("status") == "success")
        failed = sum(1 for v in today_data.values() if v.get("status") == "failed")
        warning = sum(1 for v in today_data.values() if v.get("status") == "warning")

        # 最近7天日期
        days = []
        for i in range(6, -1, -1):
            d = (datetime.now() - _td(days=i)).strftime("%Y-%m-%d")
            days.append(d)

        # 构建站点列表（今天的 + 历史出现过的）
        all_sites = set(today_data.keys())
        for d in days:
            all_sites.update(history.get("history", {}).get(d, {}).keys())
        all_sites = sorted(all_sites)

        # 状态颜色映射
        status_color = {"success": "green", "failed": "red", "warning": "orange"}
        status_icon = {"success": "✅", "failed": "❌", "warning": "⚠️"}

        # 构建表格行
        table_rows = []
        for site in all_sites:
            today_info = today_data.get(site, {"status": "none", "message": ""})
            today_status = today_info.get("status", "none")
            today_msg = today_info.get("message", "")

            # 7天历史状态点
            history_dots = []
            for d in days:
                day_info = history.get("history", {}).get(d, {}).get(site, {})
                day_status = day_info.get("status", "none")
                color = status_color.get(day_status, "grey")
                icon = status_icon.get(day_status, "⚪")
                history_dots.append({
                    'component': 'VTooltip',
                    'props': {'text': f'{d}: {day_info.get("message", day_status)}'},
                    'content': [{
                        'component': 'VChip',
                        'props': {'color': color, 'size': 'x-small', 'variant': 'flat', 'label': True},
                        'content': [{'component': 'span', 'props': {'text': icon}}]
                    }]
                })

            row = {
                'component': 'VTableRow',
                'content': [
                    {'component': 'VTableCell', 'content': [{'component': 'span', 'props': {'text': site, 'class': 'font-medium'}}]},
                    {'component': 'VTableCell', 'content': [{
                        'component': 'VChip',
                        'props': {'color': status_color.get(today_status, 'grey'), 'size': 'small', 'variant': 'tonal'},
                        'content': [{'component': 'span', 'props': {'text': f'{status_icon.get(today_status, "⚪")} {today_msg or today_status}'}}]
                    }]},
                    {'component': 'VTableCell', 'content': history_dots},
                ]
            }
            table_rows.append(row)

        # 页面组件
        page = [
            # 统计卡片行
            {
                'component': 'VRow',
                'props': {'class': 'mb-4'},
                'content': [
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{
                        'component': 'VCard',
                        'props': {'variant': 'tonal', 'color': 'primary', 'class': 'pa-4'},
                        'content': [
                            {'component': 'div', 'props': {'text': '今日签到', 'class': 'text-caption text-medium-emphasis'}},
                            {'component': 'div', 'props': {'text': f'{success}/{total}', 'class': 'text-h5 font-bold mt-1'}},
                            {'component': 'div', 'props': {'text': f'失败{failed} · 异常{warning}', 'class': 'text-caption mt-1'}},
                        ]
                    }]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{
                        'component': 'VCard',
                        'props': {'variant': 'tonal', 'color': 'success', 'class': 'pa-4'},
                        'content': [
                            {'component': 'div', 'props': {'text': '签到成功', 'class': 'text-caption text-medium-emphasis'}},
                            {'component': 'div', 'props': {'text': f'{success}', 'class': 'text-h5 font-bold mt-1'}},
                        ]
                    }]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{
                        'component': 'VCard',
                        'props': {'variant': 'tonal', 'color': 'error', 'class': 'pa-4'},
                        'content': [
                            {'component': 'div', 'props': {'text': '签到失败', 'class': 'text-caption text-medium-emphasis'}},
                            {'component': 'div', 'props': {'text': f'{failed}', 'class': 'text-h5 font-bold mt-1'}},
                        ]
                    }]},
                    {'component': 'VCol', 'props': {'cols': 12, 'md': 3}, 'content': [{
                        'component': 'VCard',
                        'props': {'variant': 'tonal', 'color': 'info', 'class': 'pa-4'},
                        'content': [
                            {'component': 'div', 'props': {'text': '历史记录', 'class': 'text-caption text-medium-emphasis'}},
                            {'component': 'div', 'props': {'text': f'{len(history.get("history", {}))}天', 'class': 'text-h5 font-bold mt-1'}},
                            {'component': 'div', 'props': {'text': '最近7天详情见下表', 'class': 'text-caption mt-1'}},
                        ]
                    }]},
                ]
            },
            # 签到状态表格
            {
                'component': 'VCard',
                'props': {'variant': 'outlined', 'class': 'pa-4'},
                'content': [
                    {'component': 'div', 'props': {'text': '签到状态（最近7天）', 'class': 'text-subtitle-1 font-bold mb-3'}},
                    {
                        'component': 'VTable',
                        'props': {'density': 'comfortable', 'hover': True},
                        'content': [
                            {
                                'component': 'thead',
                                'content': [{
                                    'component': 'VTableRow',
                                    'content': [
                                        {'component': 'VTableHeader', 'props': {'text': '站点'}},
                                        {'component': 'VTableHeader', 'props': {'text': '今日状态'}},
                                        {'component': 'VTableHeader', 'props': {'text': '近7天历史（从左到右：6天前→今天）'}},
                                    ]
                                }]
                            },
                            {
                                'component': 'tbody',
                                'content': table_rows if table_rows else [{
                                    'component': 'VTableRow',
                                    'content': [{'component': 'VTableCell', 'props': {'text': '暂无签到记录，请先运行一次签到', 'colspan': '3', 'class': 'text-center text-medium-emphasis'}}]
                                }]
                            }
                        ]
                    }
                ]
            },
            # 底部提示
            {
                'component': 'VAlert',
                'props': {'type': 'info', 'variant': 'tonal', 'text': '站点自动签到Pro v3.0.0：并发队列+失败重试+智能降级(普通请求→FlareSolverr→Playwright)+详细数据统计。历史记录保存在 /config/prosignin_history.json，保留最近30天。', 'class': 'mt-4'}
            }
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
                    logger.info(f"{site.name}: 第{attempt}次重试")
                    _time.sleep(3)

                site_name = site.name
                site_url = site.url
                site_cookie = site.cookie
                if not site_cookie:
                    return f"❌ {site_name}: 无Cookie"

                sign_url = f"{site_url.rstrip('/')}/attendance.php"
                page_source = None

                # 第1步：普通请求（最快）
                try:
                    resp = RequestUtils(cookies=site_cookie, timeout=30).get_res(url=sign_url)
                    if resp and resp.status_code == 200:
                        page_source = resp.text
                except Exception as e:
                    logger.warning(f"普通请求失败: {e}")

                # 第2步：检测CF挑战，降级FlareSolverr
                if page_source and under_challenge(page_source) and self._auto_cf >= 1:
                    logger.info(f"{site_name}: 检测到CF挑战，降级FlareSolverr")
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
                        logger.warning(f"FlareSolverr失败: {e}")

                # 第3步：仍有CF挑战，降级Playwright
                if page_source and under_challenge(page_source) and self._auto_cf >= 2:
                    logger.info(f"{site_name}: FlareSolverr未过CF，降级Playwright")
                    page_source = None
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
                    result = f"❌ {site_name}: 请求失败"
                elif under_challenge(page_source):
                    result = f"⚠️ {site_name}: CF挑战"
                elif not SiteUtils.is_logged_in(page_source):
                    result = f"❌ {site_name}: Cookie失效"
                elif "签到成功" in page_source or "已签到" in page_source or SiteUtils.is_checkin(page_source):
                    result = f"✅ {site_name}: 签到成功"
                else:
                    result = f"✅ {site_name}: 请求已发送"

                # 重试判断：如果结果包含重试关键词，且不是最后一次尝试
                if attempt < max_retries and retry_keywords:
                    if any(kw in result for kw in retry_keywords):
                        logger.info(f"{site_name}: 结果命中重试关键词，准备重试")
                        continue
                return result

            except Exception as e:
                logger.error(f"{site.name}: 异常 {e}")
                if attempt < max_retries:
                    continue
                return f"❌ {site.name}: 异常 {str(e)[:50]}"

        return f"❌ {site.name}: 重试次数耗尽"

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
            logger.info(f"清理本插件缓存完成，清理{len(cleaned)}个文件")
        except Exception as e:
            logger.warning(f"清理缓存失败: {e}")
        return cleaned

    def __load_history(self):
        """加载签到历史记录"""
        try:
            if os.path.exists(self._history_file):
                with open(self._history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.warning(f"加载签到历史失败: {e}")
        return {"history": {}}

    def __save_history(self, history):
        """保存签到历史记录，只保留最近30天"""
        try:
            # 只保留最近30天
            if "history" in history:
                cutoff = (datetime.now() - _td(days=30)).strftime("%Y-%m-%d")
                history["history"] = {k: v for k, v in history["history"].items() if k >= cutoff}
            with open(self._history_file, 'w', encoding='utf-8') as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存签到历史失败: {e}")

    def __record_results(self, results):
        """记录本次签到结果到历史"""
        history = self.__load_history()
        today = datetime.now().strftime("%Y-%m-%d")
        if "history" not in history:
            history["history"] = {}
        history["history"][today] = {}
        for result in results:
            # 解析结果格式: ✅ 站点名: 原因 或 ❌ 站点名: 原因
            if ":" in result:
                status_icon = result[0]
                rest = result[1:].strip()
                if ":" in rest:
                    site_name, message = rest.split(":", 1)
                    site_name = site_name.strip()
                    message = message.strip()
                else:
                    site_name = rest
                    message = ""
                status = "success" if status_icon == "✅" else ("warning" if status_icon == "⚠️" else "failed")
                history["history"][today][site_name] = {
                    "status": status,
                    "message": message
                }
        self.__save_history(history)

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
            logger.info("没有站点")
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

        logger.info(f"开始签到，共{len(sign_sites)}个站点，并发数={self._queue_cnt}")
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
                    logger.error(f"{site.name}: 并发异常 {e}")
                    results.append(f"❌ {site.name}: 异常 {str(e)[:50]}")

        # 按站点名称排序输出
        results.sort()

        # 记录到历史
        self.__record_results(results)

        if self._notify:
            notify_text = "站点签到结果：\n" + "\n".join(results)
            logger.info(notify_text)
            try:
                from app.core.notify import post_message
                post_message(channel=NotificationType.Wechat, title="站点自动签到Pro", text=notify_text)
            except Exception as e:
                logger.warning(f"通知发送失败: {e}")
