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