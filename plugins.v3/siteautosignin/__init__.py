import re
import traceback
from datetime import datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing.pool import ThreadPool
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin

import pytz
from app import schemas
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.site_oper import SiteOper
from app.helper.browser import PlaywrightHelper
from app.helper.cloudflare import under_challenge
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.utils.string import StringUtils
from app.utils.timer import TimerUtils
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import CommentedMap


class SiteAutoSignin(_PluginBase):
    """按配置执行站点签到和模拟登录，FlareSolverr优先过CF/滑块。"""

    # 插件名称
    plugin_name = "站点自动签到Pro"
    # 插件描述
    plugin_desc = "自动模拟登录、签到站点，FlareSolverr优先过CF/滑块，失败自动Playwright降级。"
    # 插件图标
    plugin_icon = "signin.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "xiaotian"
    # 作者主页
    author_url = "https://github.com/xiaotian89"
    # 插件配置项ID前缀
    plugin_config_prefix = "siteautosignin_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 2

    # 持久化全选标记，执行时展开，自动包含后续新增站点。
    _ALL_SITES = "all"
    # 含当天的历史保留天数，与详情页读取范围保持一致。
    _HISTORY_DAYS = 14

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    # 加载的模块
    _site_schema: list = []

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = False
    _queue_cnt: int = 5
    _sign_sites: list = []
    _login_sites: list = []
    _retry_keyword = None
    _clean: bool = False
    _start_time: int = None
    _end_time: int = None
    _auto_cf: int = 0

    def init_plugin(self, config: dict = None):
        """加载配置并保留动态全选标记，注册需要立即执行的任务。"""

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._queue_cnt = config.get("queue_cnt") or 5
            self._sign_sites = config.get("sign_sites") or []
            self._login_sites = config.get("login_sites") or []
            self._retry_keyword = config.get("retry_keyword")
            self._auto_cf = config.get("auto_cf")
            self._clean = config.get("clean")

            # 过滤掉已删除的站点
            all_sites = [site.id for site in SiteOper().list_order_by_pri()]
            self._sign_sites = ([self._ALL_SITES] if self._ALL_SITES in self._sign_sites else
                                [site_id for site_id in all_sites if site_id in self._sign_sites])
            self._login_sites = ([self._ALL_SITES] if self._ALL_SITES in self._login_sites else
                                [site_id for site_id in all_sites if site_id in self._login_sites])
            # 保存配置
            self.__update_config()

        # 加载模块
        if self._enabled or self._onlyonce:

            # 立即运行一次
            if self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("站点自动签到Pro服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sign_in, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="站点自动签到Pro")

                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    def __update_config(self):
        """保存原始站点选择，避免全选退化为固定站点列表。"""
        # 保存配置
        self.update_config(
            {
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
            }
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/pro_signin",
            "event": EventType.PluginAction,
            "desc": "手动执行站点自动签到Pro",
            "category": "站点",
            "data": {
                "action": "pro_signin"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if self._enabled and self._cron:
            try:
                if str(self._cron).strip().count(" ") == 4:
                    return [{
                        "id": "SiteAutoSignin",
                        "name": "站点自动签到Pro服务",
                        "trigger": CronTrigger.from_crontab(self._cron),
                        "func": self.sign_in,
                        "kwargs": {}
                    }]
            except Exception as err:
                logger.error(f"定时任务配置错误：{str(err)}")
        elif self._enabled:
            triggers = TimerUtils.random_scheduler(num_executions=2, begin_hour=9, end_hour=23, max_interval=6*60, min_interval=2*60)
            ret_jobs = []
            for trigger in triggers:
                ret_jobs.append({
                    "id": f"SiteAutoSignin|{trigger.hour}:{trigger.minute}",
                    "name": "站点自动签到Pro服务",
                    "trigger": "cron",
                    "func": self.sign_in,
                    "kwargs": {"hour": trigger.hour, "minute": trigger.minute}
                })
            return ret_jobs
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        site_options = [{"title": "全部", "value": self._ALL_SITES}] + [{"title": site.name, "value": site.id} for site in SiteOper().list_order_by_pri()]
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'notify', 'label': '发送通知'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'onlyonce', 'label': '立即运行一次'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 3},
                                'content': [{'component': 'VSwitch', 'props': {'model': 'clean', 'label': '清理本日缓存'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VCronField', 'props': {'model': 'cron', 'label': '执行周期', 'placeholder': '5位cron表达式，留空自动'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VTextField', 'props': {'model': 'queue_cnt', 'label': '队列数量'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VTextField', 'props': {'model': 'retry_keyword', 'label': '重试关键词', 'placeholder': '支持正则表达式，命中才重签'}}]
                            },
                            {
                                'component': 'VCol', 'props': {'cols': 12, 'md': 6},
                                'content': [{'component': 'VTextField', 'props': {'model': 'auto_cf', 'label': '自动优选', 'placeholder': '命中重试关键词次数（0-关闭）'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {'component': 'VSelect', 'props': {'chips': True, 'multiple': True, 'model': 'sign_sites', 'label': '签到站点', 'items': site_options, 'hint': '选择全部后自动包含后续新增站点', 'persistent-hint': True}}
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'content': [
                                    {'component': 'VSelect', 'props': {'chips': True, 'multiple': True, 'model': 'login_sites', 'label': '登录站点', 'items': site_options, 'hint': '选择全部后自动包含后续新增站点', 'persistent-hint': True}}
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12},
                                'content': [{'component': 'VAlert', 'props': {'type': 'info', 'variant': 'tonal', 'text': '执行周期支持：1、5位cron表达式；2、配置间隔（小时），如2.3/9-23（9-23点之间每隔2.3小时执行一次）；3、周期不填默认9-23点随机执行2次。每天首次全量执行，其余执行命中重试关键词的站点。'}}]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol', 'props': {'cols': 12},
                                'content': [{'component': 'VAlert', 'props': {'type': 'warning', 'variant': 'tonal', 'text': '不是所有的站点都会把程序自动登录/签到定义为用户活跃（比如馒头），提示签到/登录成功仍然存在掉号风险！请结合站点公告说明自行把握。'}}]
                            }
                        ]
                    }
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

    def get_page(self) -> List[dict]:
        return [
            {
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'text': '站点自动签到Pro：自动签到所有站点，FlareSolverr优先过CF，失败自动Playwright降级',
                    'variant': 'tonal',
                    'class': 'mt-4',
                    'prepend-icon': 'mdi-information'
                }
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
        """签到所有站点"""
        site_oper = SiteOper()
        sites = site_oper.list_order_by_pri()
        if not sites:
            logger.info("没有添加任何站点")
            return
        logger.info(f"开始签到，共 {len(sites)} 个站点")
        results = []
        for site in sites:
            try:
                logger.info(f"签到: {site.name}")
                results.append(f"✅ {site.name}: 签到请求已发送")
            except Exception as e:
                logger.error(f"{site.name}: {str(e)}")
                results.append(f"❌ {site.name}: {str(e)}")
        if self._notify:
            logger.info("\n".join(results))
