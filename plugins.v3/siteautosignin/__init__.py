# -*- coding: utf-8 -*-
"""站点自动签到Pro - MoviePilot V3

自动签到MP里所有已添加站点，FlareSolverr优先过CF，失败自动降级Playwright浏览器渲染。
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime
from typing import Any, Optional

import httpx2
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.plugins import _PluginBase
from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.schemas.types import EventType, NotificationChannel


class SiteAutoSignin(_PluginBase):
    """站点自动签到Pro。"""

    plugin_name = "站点自动签到Pro"
    plugin_desc = "自动签到MP里所有已添加站点，FlareSolverr+Playwright自动过CF滑块，随机错峰，微信通知。"
    plugin_icon = "https://img.icons8.com/fluency/96/calendar.png"
    plugin_version = "1.5.0"
    plugin_author = "xiaotian"
    author_url = "https://github.com/xiaotian89"
    plugin_config_prefix = "siteautosignin_"
    plugin_order = 20
    auth_level = 1

    _enabled = False
    _cron = "0 8 * * *"
    _notify = True
    _delay_seconds = 1800
    _onlyonce = False
    _auto_cf = True
    _sign_sites = []
    _flaresolverr_url = "http://192.168.2.70:8191/v1"
    _scheduler = None

    def init_plugin(self, config: Optional[dict] = None) -> None:
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._cron = str(config.get("cron") or "0 8 * * *").strip()
        self._notify = bool(config.get("notify", True))
        self._delay_seconds = int(config.get("delay_seconds") or 1800)
        if self._delay_seconds > 7200:
            self._delay_seconds = 7200
        self._onlyonce = bool(config.get("onlyonce", False))
        self._auto_cf = bool(config.get("auto_cf", True))
        self._sign_sites = config.get("sign_sites", []) or []
        self._flaresolverr_url = str(config.get("flaresolverr_url") or "http://192.168.2.70:8191/v1").strip()

        if self._onlyonce and self._enabled:
            logger.info("站点自动签到Pro：收到保存后运行一次请求，3秒后执行")
            self._onlyonce = False
            self.__update_config()
            try:
                self._scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
                self._scheduler.add_job(
                    func=lambda: self._safe_run(self._run_sign(skip_delay=True)),
                    trigger="date",
                    run_date=datetime.now().astimezone() + __import__("datetime").timedelta(seconds=3),
                    name="站点自动签到立即运行",
                )
                self._scheduler.start()
            except Exception as err:
                logger.error(f"站点自动签到Pro：立即执行调度启动失败：{err}")

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "cron": self._cron,
            "delay_seconds": self._delay_seconds,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "auto_cf": self._auto_cf,
            "sign_sites": self._sign_sites,
            "flaresolverr_url": self._flaresolverr_url,
        })

    @staticmethod
    def get_command() -> list[dict[str, Any]]:
        return [
            {
                "cmd": "/pro_signin",
                "event": EventType.PluginAction,
                "desc": "手动执行站点自动签到Pro",
                "category": "插件命令",
                "data": {"action": "pro_signin"},
            }
        ]

    def get_api(self) -> list[dict[str, Any]]:
        return [
            {
                "path": "/sign_now",
                "endpoint": self.sign_now,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "立即执行一次站点自动签到",
            },
        ]

    def get_service(self) -> list[dict]:
        if not self.get_state():
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as err:
            logger.warning(f"站点自动签到Pro：定时表达式无效({err})，使用默认 08:00")
            trigger = CronTrigger.from_crontab("0 8 * * *")
        return [
            {
                "id": "SiteAutoSignin.Daily",
                "name": "站点自动签到Pro每日签到",
                "trigger": trigger,
                "func": self.schedule_sign,
                "kwargs": {},
            }
        ]

    def get_form(self) -> tuple[list[dict], dict[str, Any]]:
        # 构造站点列表
        all_sites = [{"title": "全部站点", "value": "all"}]
        try:
            from app.db.site_oper import SiteOper
            for s in SiteOper().list_order_by_pri():
                all_sites.append({"title": s.name, "value": s.id})
        except Exception:
            pass

        glass = (
            "background-color: rgba(var(--v-theme-surface), 0.72); "
            "color: rgb(var(--v-theme-on-surface)); "
            "border: 1px solid rgba(var(--v-theme-on-surface), 0.10); "
            "border-radius: 10px;"
        )

        def icon_tile(icon: str, color: str, size: int = 34) -> dict:
            return {
                "component": "div",
                "props": {
                    "class": "d-flex align-center justify-center",
                    "style": (
                        f"width: {size}px; height: {size}px; border-radius: 10px; "
                        f"background: color-mix(in srgb, {color} 14%, transparent);"
                    ),
                },
                "content": [
                    {"component": "VIcon", "props": {"size": int(size * 0.56), "color": color}, "text": icon},
                ],
            }

        def section(title: str, icon: str, color: str, content: list) -> dict:
            return {
                "component": "div",
                "props": {"class": "mb-3", "style": glass},
                "content": [
                    {
                        "component": "div",
                        "props": {
                            "class": "d-flex align-center ga-2",
                            "style": "padding: 10px 14px 6px 14px;",
                        },
                        "content": [
                            icon_tile(icon, color),
                            {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold"}, "text": title},
                        ],
                    },
                    {
                        "component": "VRow",
                        "props": {"class": "pa-2", "dense": True},
                        "content": content,
                    },
                ],
            }

        def field(model: str, label: str, placeholder: str = "", type_: str = "text", md: int = 12) -> dict:
            props: dict[str, Any] = {
                "model": model, "label": label, "placeholder": placeholder,
                "variant": "outlined", "density": "comfortable", "hide-details": True,
            }
            if type_ != "text":
                props["type"] = type_
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": md},
                "content": [{"component": "VTextField", "props": props}],
            }

        def switch(model: str, label: str, color: str, hint: str = "", md: int = 6) -> dict:
            props: dict[str, Any] = {"model": model, "label": label, "color": color, "hide-details": True}
            if hint:
                props["hint"] = hint
                props["persistent-hint"] = True
            return {
                "component": "VCol",
                "props": {"cols": 12, "md": md},
                "content": [{"component": "VSwitch", "props": props}],
            }

        return [
            {
                "component": "VForm",
                "content": [
                    section("启用", "mdi-power", "#4CAF50", [switch("enabled", "启用插件", "#4CAF50", "开启后按定时任务自动签到所有站点", 12)]),
                    section(
                        "签到设置",
                        "mdi-clock-outline",
                        "#FF9800",
                        [
                            field("cron", "签到时间(Cron)", "默认 0 8 * * *（每天08:00）", md=6),
                            field("delay_seconds", "随机错峰秒数(0-7200)", "默认1800：定时触发后随机延迟0-30分钟", md=6),
                            switch("auto_cf", "自动过CF/滑块(推荐)", "#4CAF50", "优先调用FlareSolverr，失败自动降级Playwright浏览器渲染"),
                            switch("notify", "签到结果通知", "#2196F3", "签到结果推送到 MP 全局微信 ClawBot"),
                            switch("onlyonce", "保存后立即执行一次", "#9C27B0", "勾选后保存配置，3秒后自动执行一次签到（执行后自动复位）"),
                        ],
                    ),
                    section(
                        "站点选择",
                        "mdi-web",
                        "#2196F3",
                        [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "sign_sites",
                                            "label": "签到站点（留空或选全部=所有站点）",
                                            "items": all_sites,
                                            "chips": True,
                                            "multiple": True,
                                            "variant": "outlined",
                                            "density": "comfortable",
                                            "hide-details": True,
                                            "placeholder": "默认签到所有已添加站点",
                                        },
                                    }
                                ],
                            },
                            field("flaresolverr_url", "FlareSolverr地址", "默认 http://192.168.2.70:8191/v1", md=12),
                        ],
                    ),
                ],
            }
        ], {
            "enabled": self._enabled,
            "cron": self._cron,
            "delay_seconds": self._delay_seconds,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "auto_cf": self._auto_cf,
            "sign_sites": self._sign_sites,
            "flaresolverr_url": self._flaresolverr_url,
        }

    def get_page(self) -> list[dict]:
        history = self.get_history_sync()
        total = len(history)
        success = sum(1 for h in history if isinstance(h, dict) and h.get("result") == "成功")
        last = history[-1] if history and isinstance(history[-1], dict) else {}
        last_time = str(last.get("time") or "—")[:16]
        last_result = last.get("result") if last else "暂无"
        last_detail = last.get("detail") if last else "保存配置后等待定时任务或点击立即签到"

        glass = (
            "background-color: rgba(var(--v-theme-surface), 0.72); "
            "color: rgb(var(--v-theme-on-surface)); "
            "border: 1px solid rgba(var(--v-theme-on-surface), 0.10); "
            "border-radius: 10px;"
        )

        def stat(icon: str, label: str, value: str, color: str) -> dict:
            return {
                "component": "VCol",
                "props": {"cols": 6, "md": 3, "class": "pa-1"},
                "content": [
                    {
                        "component": "div",
                        "props": {
                            "class": "d-flex flex-column align-center justify-center pa-2",
                            "style": glass,
                        },
                        "content": [
                            {
                                "component": "div",
                                "props": {"class": "d-flex align-center justify-center ga-1"},
                                "content": [
                                    {"component": "VIcon", "props": {"size": 15, "color": color}, "text": icon},
                                    {"component": "span", "props": {"class": "text-subtitle-2 font-weight-bold"}, "text": str(value)},
                                ],
                            },
                            {"component": "div", "props": {"class": "text-caption text-medium-emphasis mt-1"}, "text": label},
                        ],
                    }
                ],
            }

        history_rows = []
        for item in history[-10:]:
            ok = item.get("result") == "成功"
            history_rows.append(
                {
                    "component": "div",
                    "props": {"class": "d-flex align-center ga-3", "style": f"{glass} padding: 8px 12px; margin-bottom: 6px;"},
                    "content": [
                        {
                            "component": "div",
                            "props": {"class": "d-flex align-center justify-center flex-shrink-0",
                                      "style": f"width: 30px; height: 30px; border-radius: 50%; background: color-mix(in srgb, {'#4CAF50' if ok else '#F44336'} 14%, transparent);"},
                            "content": [
                                {"component": "VIcon", "props": {"size": 16, "color": "#4CAF50" if ok else "#F44336"},
                                 "text": "mdi-check-circle" if ok else "mdi-close-circle"},
                            ],
                        },
                        {
                            "component": "div",
                            "props": {"class": "flex-grow-1", "style": "min-width: 0;"},
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "text-body-2"},
                                    "content": [
                                        {"component": "span", "props": {"class": "font-weight-bold"}, "text": str(item.get("time") or "")[:16]},
                                        {"component": "span", "props": {"class": "text-medium-emphasis"}, "text": f"  {item.get('message') or ''}"},
                                    ],
                                },
                                {"component": "div", "props": {"class": "text-caption text-medium-emphasis", "style": "overflow: hidden; text-overflow: ellipsis; white-space: nowrap;"},
                                 "text": str(item.get("detail") or "")},
                            ],
                        },
                    ],
                }
            )
        if not history_rows:
            history_rows = [
                {
                    "component": "div",
                    "props": {"class": "text-caption text-medium-emphasis pa-4", "style": glass},
                    "text": "暂无签到记录，保存配置后等待定时任务或点击立即签到。",
                }
            ]

        return [
            {
                "component": "div",
                "props": {"class": "d-flex align-center ga-3 mb-2", "style": f"{glass} padding: 12px 16px;"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "d-flex align-center justify-center",
                                  "style": "width: 42px; height: 42px; border-radius: 12px; background: color-mix(in srgb, #2196F3 14%, transparent);"},
                        "content": [{"component": "VIcon", "props": {"size": 22, "color": "#2196F3"}, "text": "mdi-calendar-check"}],
                    },
                    {
                        "component": "div",
                        "props": {"class": "flex-grow-1"},
                        "content": [
                            {"component": "div", "props": {"class": "text-body-2 font-weight-bold"}, "text": "站点自动签到Pro"},
                            {
                                "component": "div",
                                "props": {"class": "text-caption text-medium-emphasis mt-1"},
                                "text": f"定时：{self._cron} ｜ 错峰：{self._delay_seconds}s ｜ CF自动：{'开启' if self._auto_cf else '关闭'} ｜ 通知：{'开启' if self._notify else '关闭'}",
                            },
                        ],
                    },
                ],
            },
            {
                "component": "VRow",
                "props": {"dense": True, "class": "mb-1"},
                "content": [
                    stat("mdi-check-circle", "最近结果", last_result, "#4CAF50" if last_result == "成功" else "#FF9800"),
                    stat("mdi-clock-outline", "最近时间", last_time, "#2196F3"),
                    stat("mdi-history", "签到次数", total, "#9C27B0"),
                    stat("mdi-trophy", "成功次数", success, "#FF9800"),
                ],
            },
            {
                "component": "div",
                "props": {"class": "text-caption text-medium-emphasis mb-2", "style": f"{glass} padding: 8px 12px;"},
                "text": f"最近详情：{last_detail}",
            },
            {
                "component": "div",
                "props": {"class": "mt-1"},
                "content": [
                    {"component": "div", "props": {"class": "text-subtitle-2 font-weight-bold mb-1"}, "text": "签到历史（最近10条）"},
                    {"component": "div", "content": history_rows},
                ],
            },
        ]

    def stop_service(self) -> None:
        self._enabled = False
        try:
            if self._scheduler:
                self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception:
            pass

    @eventmanager.register(EventType.PluginAction)
    def _on_plugin_action(self, event: Event) -> None:
        event_data = getattr(event, "event_data", None) or {}
        if event_data.get("action") != "pro_signin":
            return
        self._safe_run(self._run_sign())

    async def sign_now(self) -> dict:
        if not self._enabled:
            return {"success": False, "message": "插件未启用"}
        result = await self._run_sign(skip_delay=True)
        return result

    @staticmethod
    def _safe_run(coro) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
            return
        except RuntimeError:
            asyncio.run(coro)

    def schedule_sign(self) -> None:
        self._safe_run(self._run_sign(skip_delay=False))

    async def _run_sign(self, skip_delay: bool = False) -> dict:
        result = {"success": False, "message": "", "detail": ""}
        try:
            if self._delay_seconds > 0 and not skip_delay:
                wait = random.randint(0, self._delay_seconds)
                if wait > 0:
                    logger.info(f"站点自动签到Pro：随机错峰延迟 {wait} 秒")
                    await asyncio.sleep(wait)

            outcome = await self._do_sign()
            result.update(outcome)
        except Exception as err:
            logger.error(f"站点自动签到Pro异常：{err}", exc_info=True)
            result["message"] = "签到异常"
            result["detail"] = str(err)

        self._record(result)
        if self._notify:
            try:
                self.post_message(
                    channel=NotificationChannel.WechatClawBot,
                    title=f"站点自动签到{'成功' if result['success'] else '失败'}",
                    text=f"结果：{result['message']}\n详情：{result['detail']}",
                )
            except Exception as err:
                logger.warning(f"站点自动签到Pro通知发送失败：{err}")
        return result

    async def _get_flaresolverr_page(self, url: str, cookie: str) -> Optional[str]:
        """调用FlareSolverr过CF，返回页面源码。"""
        try:
            payload = {
                "cmd": "request.get",
                "url": url,
                "maxTimeout": 60000,
                "headers": {"Cookie": cookie}
            }
            async with httpx2.AsyncClient(timeout=70.0) as client:
                resp = await client.post(self._flaresolverr_url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("status") == "ok":
                        return data.get("solution", {}).get("response", "")
            return None
        except Exception as e:
            logger.warning(f"FlareSolverr请求失败: {e}")
            return None

    async def _get_playwright_page(self, url: str, cookie: str) -> Optional[str]:
        """降级用Playwright浏览器渲染。"""
        try:
            from app.helper.browser import PlaywrightHelper
            page = PlaywrightHelper().get_page_source(
                url=url,
                cookies=cookie,
                ua="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                timeout=20
            )
            return page
        except Exception as e:
            logger.warning(f"Playwright渲染失败: {e}")
            return None

    async def _do_sign(self) -> dict:
        """核心签到：遍历选中站点，FlareSolverr优先过CF，失败降级Playwright。"""
        try:
            from app.db.site_oper import SiteOper
            from app.helper.cloudflare import under_challenge
            from app.utils.site import SiteUtils

            all_sites = SiteOper().list_order_by_pri()
            if not all_sites:
                return {"success": False, "message": "没有添加任何站点", "detail": "请先在站点管理中添加PT站点"}

            # 筛选要签到的站点
            selected_ids = set(self._sign_sites or [])
            if not selected_ids or "all" in selected_ids:
                sites = all_sites
            else:
                sites = [s for s in all_sites if s.id in selected_ids]

            if not sites:
                return {"success": False, "message": "没有选中任何站点", "detail": "请在配置中选择要签到的站点"}

            success_count = 0
            fail_count = 0
            details = []
            for site in sites:
                try:
                    await asyncio.sleep(random.uniform(2, 8))
                    site_name = site.name
                    site_url = site.url
                    site_cookie = site.cookie
                    if not site_cookie:
                        details.append(f"{site_name}: 无Cookie")
                        fail_count += 1
                        continue

                    logger.info(f"站点自动签到Pro：签到 {site_name}")
                    sign_url = f"{site_url.rstrip('/')}/attendance.php"
                    page_source = None

                    # 1. 优先FlareSolverr过CF
                    if self._auto_cf:
                        page_source = await self._get_flaresolverr_page(sign_url, site_cookie)

                    # 2. 失败降级普通请求
                    if not page_source:
                        try:
                            async with httpx2.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                                resp = await client.get(sign_url, headers={
                                    "Cookie": site_cookie,
                                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                                })
                                if resp.status_code == 200:
                                    page_source = resp.text
                        except Exception:
                            pass

                    # 3. 还是失败且开启了自动CF，降级Playwright
                    if not page_source and self._auto_cf:
                        page_source = await self._get_playwright_page(sign_url, site_cookie)

                    if not page_source:
                        details.append(f"{site_name}: 请求失败")
                        fail_count += 1
                        continue

                    # 判定结果
                    if under_challenge(page_source):
                        details.append(f"{site_name}: CF挑战未通过")
                        fail_count += 1
                        continue
                    if not SiteUtils.is_logged_in(page_source):
                        details.append(f"{site_name}: Cookie失效")
                        fail_count += 1
                        continue
                    if "签到成功" in page_source or "已签到" in page_source or SiteUtils.is_checkin(page_source):
                        details.append(f"{site_name}: 签到成功")
                        success_count += 1
                    else:
                        details.append(f"{site_name}: 请求已发送")
                        success_count += 1

                except Exception as e:
                    details.append(f"{site.name}: 异常 {str(e)[:50]}")
                    fail_count += 1

            total = len(sites)
            return {
                "success": success_count > 0,
                "message": f"完成 {success_count}/{total} 个站点",
                "detail": "；".join(details[:5]) + ("..." if len(details) > 5 else "")
            }
        except Exception as e:
            return {"success": False, "message": "签到流程异常", "detail": str(e)[:100]}

    def _record(self, result: dict) -> None:
        try:
            history = self.get_history_sync()
            history.append(
                {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "result": "成功" if result["success"] else "失败",
                    "message": result["message"],
                    "detail": result["detail"],
                }
            )
            self.save_data("history", history[-30:])
        except Exception as err:
            logger.warning(f"站点自动签到Pro记录历史失败：{err}")

    def get_history_sync(self) -> list[dict]:
        try:
            data = self.get_data("history")
            return list(data or [])
        except Exception:
            return []
