import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, Dict, List, Optional, Self, Tuple

import pytz
import requests
from app.core.config import settings
from app.core.event import eventmanager
from app.db.models import Site
from app.db.site_oper import SiteOper
from app.helper.sites import SitesHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.timer import TimerUtils
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from bencode import dumps, loads


class CSSiteConfig(object):
    """
    站点辅种配置类
    """

    def __init__(self, site_name: str, site_url: str, site_passkey: str) -> None:
        self.name = site_name
        self.url = site_url.removesuffix("/")
        self.passkey = site_passkey

    def get_api_url(self):
        if self.name == "憨憨":
            return f"{self.url}/npapi/pieces-hash"
        return f"{self.url}/api/pieces-hash"

    def get_torrent_url(self, torrent_id: str):
        return f"{self.url}/download.php?id={torrent_id}&passkey={self.passkey}"


class TorInfo:

    def __init__(
        self,
        site_name: str = None,
        torrent_path: str = None,
        file_path: str = None,
        info_hash: str = None,
        pieces_hash: str = None,
        torrent_id: str = None,
    ) -> None:
        self.site_name = site_name
        self.torrent_path = torrent_path
        self.file_path = file_path
        self.info_hash = info_hash
        self.pieces_hash = pieces_hash
        self.torrent_id = torrent_id
        self.torrent_announce = None

    @staticmethod
    def local(torrent_path: str, info_hash: str, pieces_hash: str) -> Self:

        return TorInfo(
            torrent_path=torrent_path, info_hash=info_hash, pieces_hash=pieces_hash
        )

    @staticmethod
    def remote(site_name: str, pieces_hash: str, torrent_id: str) -> Self:
        return TorInfo(
            site_name=site_name, pieces_hash=pieces_hash, torrent_id=torrent_id
        )

    @staticmethod
    def from_data(data: bytes) -> tuple[Self, str]:
        try:
            torrent = loads(data)
            info = torrent["info"]
            pieces = info["pieces"]
            info_hash = hashlib.sha1(dumps(info)).hexdigest()
            pieces_hash = hashlib.sha1(pieces).hexdigest()
            local_tor = TorInfo(info_hash=info_hash, pieces_hash=pieces_hash)
            #从种子中获取 announce, qb可能存在获取不到的情况，会存在于fastresume文件中
            if "announce" in torrent:
                local_tor.torrent_announce  = torrent["announce"]
            return local_tor, None
        except Exception as err:
            return None, err

    def get_name_id_tag(self):
        return f"{self.site_name}:{self.torrent_id}"

    def get_name_pieces_tag(self):
        return f"{self.site_name}:{self.pieces_hash}"

class CrossSeedHelper(object):
    _version = "0.2.0"

    def get_local_torrent_info(self, torrent_path: Path | str) -> tuple[TorInfo, str]:
        try:
            torrent_data = None
            if isinstance(torrent_path, Path):
                torrent_data = torrent_path.read_bytes()
            else:
                with open(torrent_path, "rb") as f:
                    torrent_data = f.read()
            local_tor, err = TorInfo.from_data(torrent_data)
            if not local_tor:
                return None, err
            local_tor.torrent_path = str(torrent_path)
            return local_tor, ""
        except Exception as err:
            return None, err

    def get_target_torrent(
        self, site: CSSiteConfig, pieces_hash_set: list[str]
    ) -> list[TorInfo]:
        """
        返回pieces_hash对应的种子信息，包括站点id,pieces_hash,种子id
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "CrossSeedHelper",
        }
        data = {"passkey": site.passkey, "pieces_hash": pieces_hash_set}
        try:
            response = requests.post(
                site.get_api_url(), headers=headers, json=data, timeout=10
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            return None, f"站点{site.name}请求失败：{e}"
        rsp_body = response.json()

        remote_torrent_infos = []
        if isinstance(rsp_body["data"], dict):
            for pieces_hash, torrent_id in rsp_body["data"].items():
                remote_torrent_infos.append(
                    TorInfo.remote(site.name, pieces_hash, torrent_id)
                )
        return remote_torrent_infos, None

class CrossSeed(_PluginBase):
    # 插件名称
    plugin_name = "青蛙辅种助手"
    # 插件描述
    plugin_desc = "参考ReseedPuppy和IYUU辅种插件实现自动辅种，支持站点：青蛙、AGSVPT、麒麟、UBits、聆音等。"
    # 插件图标
    plugin_icon = "qingwa.png"
    # 插件版本
    plugin_version = "1.6.1"
    # 插件作者
    plugin_author = "233@qingwa"
    # 作者主页
    author_url = "https://new.qingwa.pro/"
    # 插件配置项ID前缀
    plugin_config_prefix = "cross_seed_"
    # 加载顺序
    plugin_order = 17
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _scheduler = None
    cross_helper = None
    qb = None
    tr = None
    sites = None
    siteoper = None
    torrent = None
    # 开关
    _enabled = False
    _cron = None
    _onlyonce = False
    _token = None
    _downloaders = []
    _sites = []
    _torrentpath = None   
    _notify = False
    _nolabels = None
    _nopaths = None
    _clearcache = False
    # 退出事件
    _event = Event()
    _torrent_tags = ["已整理", "辅种"]
    # 待校全种子hash清单
    _recheck_torrents = {}
    _is_recheck_running = False
    # 辅种缓存，出错的种子不再重复辅种，可清除
    _error_caches = []
    # 辅种缓存，辅种成功的种子，可清除
    _success_caches = []
    # 辅种缓存，出错的种子不再重复辅种，且无法清除。种子被删除404等情况
    _permanent_error_caches = []
    # 辅种计数
    total = 0
    realtotal = 0
    success = 0
    exist = 0
    fail = 0
    cached = 0

    def init_plugin(self, config: dict = None):
        self.sites = SitesHelper()
        self.siteoper = SiteOper()
        self.torrent = TorrentHelper()
        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._token = config.get("token")  # passkey格式  青蛙:xxxxxx,站点名称:xxxxxxx

            self._downloaders = config.get("downloaders")
            self._torrentpath = config.get("torrentpath") #种子路径和下载器对应  /qb,/tr
            self._torrentpaths = self._torrentpath.split(",") 
            self._sites = config.get("sites") or []
            self._notify = config.get("notify")
            self._nolabels = config.get("nolabels")
            self._nopaths = config.get("nopaths")
            self._clearcache = config.get("clearcache")
            self._permanent_error_caches = [] if self._clearcache else config.get("permanent_error_caches") or []
            self._error_caches = [] if self._clearcache else config.get("error_caches") or []
            self._success_caches = [] if self._clearcache else config.get("success_caches") or []

            # 过滤掉已删除的站点
            inner_site_list = self.siteoper.list_order_by_pri()
            all_sites = [(site.id, site.name) for site in inner_site_list] + [
                (site.get("id"), site.get("name")) for site in self.__custom_sites()
            ]
            self._sites = [site_id for site_id, site_name in all_sites if site_id in self._sites]
            # 拆分出选中的站点
            site_names =  [site_name for site_id, site_name in all_sites if site_id in self._sites]
            # 拆分为映射关系
            self._name_site_map = {}
            for site in self.siteoper.list_order_by_pri():
                self._name_site_map[site.name] = site
            # 只给选中的站点构造站点配置
            self._site_cs_infos:list[CSSiteConfig] = []
            for site_key in self._token.strip().split("\n"):
                site_key_arr = site_key.strip().split(":")
                site_name = site_key_arr[0]
                db_site = self._name_site_map[site_name]
                if site_name in site_names and db_site:
                    self._site_cs_infos.append(CSSiteConfig(site_name,db_site.url,site_key_arr[1]))

            self.__update_config()

        # 停止现有任务
        self.stop_service()

        # 启动定时任务 & 立即运行一次
        if self.get_state() or self._onlyonce:
            self.cross_helper = CrossSeedHelper()
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self.qb = Qbittorrent()
            self.tr = Transmission()

            if self._onlyonce:
                logger.info(f"辅种服务启动，立即运行一次")
                self._scheduler.add_job(self.auto_seed, 'date',
                                        run_date=datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3)
                                        )

                # 关闭一次性开关
                self._onlyonce = False
                if self._scheduler.get_jobs():
                    # 追加种子校验服务
                    self._scheduler.add_job(self.check_recheck, 'interval', minutes=3)
                    # 启动服务
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._clearcache:
                # 关闭清除缓存开关
                self._clearcache = False

            if self._clearcache or self._onlyonce:
                # 保存配置
                self.__update_config()

    def get_state(self) -> bool:
        return True if self._enabled and self._cron and self._token and self._downloaders else False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron and self._token and self._downloaders and self._torrentpath:
            return [{
                "id": "CrossSeed",
                "name": "青蛙辅种助手",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.auto_seed,
                "kwargs": {}
            }]

        elif self._enabled and self._token and self._downloaders and self._torrentpath:
            # 随机时间
            triggers = TimerUtils.random_scheduler(num_executions=1,
                                                   begin_hour=2,
                                                   end_hour=7,
                                                   max_interval=290,
                                                   min_interval=0)
            ret_jobs = []
            for trigger in triggers:
                ret_jobs.append({
                    "id": f"CrossSeed|{trigger.hour}:{trigger.minute}",
                    "name": "青蛙辅种助手",
                    "trigger": "cron",
                    "func": self.auto_seed,
                    "kwargs": {
                        "hour": trigger.hour,
                        "minute": trigger.minute
                    }
                })
            return ret_jobs
        elif self._enabled:
            logger.warn(f"青蛙辅种助手插件参数不全，定时任务未正常启动")
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 站点的可选项（内置站点 + 自定义站点）
        customSites = self.__custom_sites()

        # 站点的可选项
        site_options = ([{"title": site.name, "value": site.id}
                         for site in self.siteoper.list_order_by_pri()]
                        + [{"title": site.get("name"), "value": site.get("id")}
                           for site in customSites])
        # 测试版本，只支持青蛙
        # site_options = [s for s in site_options if s["title"]=="青蛙"]

        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'sites',
                                            'label': '辅种站点',
                                            'items': site_options
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'token',
                                            'label': '站点Passkey',
                                            'rows': 3,
                                            'placeholder': '每行一个, 格式为 站点名称:Passkey ,站点名称为上面选择的名称，例如青蛙为 青蛙:xxxxxx 其中xxxxxx替换为你的Passkey'
                                        }
                                    }
                                ]
                            },
                            
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'downloaders',
                                            'label': '辅种下载器',
                                            'items': [
                                                {'title': 'Qbittorrent', 'value': 'qbittorrent'},
                                                {'title': 'Transmission', 'value': 'transmission'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 0 0 ? *'
                                        }
                                    }
                                ]
                            },
                            
                        ]
                    },
                     {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md':12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'torrentpath',
                                            'label': '种子文件目录',
                                            'placeholder': '多个目录逗号分隔，按下载器顺序对应填写，每个下载器只能有一个种子目录'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'nolabels',
                                            'label': '不辅种标签',
                                            'placeholder': '使用,分隔多个标签'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'nopaths',
                                            'label': '不辅种数据文件目录',
                                            'rows': 3,
                                            'placeholder': '每一行一个目录'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clearcache',
                                            'label': '清除缓存后运行',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '1. 定时任务周期建议每次辅种间隔时间大于1天，不填写每天上午2点到7点随机辅种一次； '
                                                    '2. 支持辅种站点列表：青蛙【已验证】，AGSVPT，麒麟，UBits，聆音 等，配置passkey时，站点名称需严格和上面选项一致，只有选中的站点会辅种，passkey可保存多个； '
                                                    '3. 请勿与IYUU辅种插件同时添加相同站点，可能会有冲突，且意义不大；'
                                                    '4. 测试站点是否支持的方法：【站点域名/api/pieces-hash】接口访问返回405则大概率支持 '
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "clearcache": False,
            "cron": "",
            "token": "",
            "downloaders": [],
            "torrentpath":"",
            "sites": [],
            "nopaths": "",
            "nolabels": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "clearcache": self._clearcache,
            "cron": self._cron,
            "token": self._token,
            "downloaders": self._downloaders,
            "torrentpath":self._torrentpath,
            "sites": self._sites,
            "notify": self._notify,
            "nolabels": self._nolabels,
            "nopaths": self._nopaths,
            "success_caches": self._success_caches,
            "error_caches": self._error_caches,
            "permanent_error_caches": self._permanent_error_caches
        })

    def __get_downloader(self, dtype: str):
        """
        根据类型返回下载器实例
        """
        if dtype == "qbittorrent":
            return self.qb
        elif dtype == "transmission":
            return self.tr
        else:
            return None

    def auto_seed(self):
        """
        开始辅种
        """
        logger.info("开始辅种任务 ...")

        # 计数器初始化
        self.total = 0
        self.realtotal = 0
        self.success = 0
        self.exist = 0
        self.fail = 0
        self.cached = 0
        # 扫描下载器辅种
        for idx, downloader in enumerate(self._downloaders):
            logger.info(f"开始扫描下载器 {downloader} ...")
            downloader_obj = self.__get_downloader(downloader)
            # 获取下载器中已完成的种子
            torrents = downloader_obj.get_completed_torrents()
            if torrents:
                logger.info(f"下载器 {downloader} 已完成种子数：{len(torrents)}")
            else:
                logger.info(f"下载器 {downloader} 没有已完成种子")
                continue   
            hash_strs = []
            for torrent in torrents:
                if self._event.is_set():
                    logger.info(f"辅种服务停止")
                    return 
                # 获取种子hash
                hash_str = self.__get_hash(torrent, downloader)
                if hash_str in self._error_caches or hash_str in self._permanent_error_caches:
                    logger.info(f"种子 {hash_str} 辅种失败且已缓存，跳过 ...")
                    continue
                save_path = self.__get_save_path(torrent, downloader)
                # 获取种子文件路径
                torrent_path = Path(self._torrentpaths[idx]) / f"{hash_str}.torrent"
                if not torrent_path.exists():
                    logger.error(f"种子文件不存在：{torrent_path}")
                    continue

                # 读取种子文件具体信息
                torrent_info, err = self.cross_helper.get_local_torrent_info(torrent_path)
                if not torrent_info:
                    logger.error(f"未能读取到种子文件具体信息：{torrent_path} {err}")
                    continue

                # 用站点+pieces_hash记录该站点是否已经在该下载器中,需要从tracker补充站点名字
                tracker_urls = set()
                try:
                    if downloader == "qbittorrent":
                        for i in torrent.trackers:
                            if "https" in i.get("url"):
                                tracker_urls.add(i.get("url"))
                    elif downloader == "transmission":
                        if torrent_info and torrent_info.torrent_announce:
                            if "https" in torrent_info.torrent_announce:
                                tracker_urls.add(torrent_info.torrent_announce)
                except Exception as err:
                    logger.warn(f"尝试获取 {downloader} 的tracker出错 {err}")
                # 根据tracker补充站点信息
                for tracker in tracker_urls:
                    # 优先通过passkey获取站点名
                    for site_config in self._site_cs_infos:
                        if site_config.passkey in tracker:
                            torrent_info.site_name = site_config.name
                            break
                    if not torrent_info.site_name:
                        # 尝试通过域名获取站点信息
                        tracker_domain = StringUtils.get_url_domain(tracker)
                        site_info = self.sites.get_indexer(tracker_domain)
                        if site_info:
                            torrent_info.site_name = site_info.get("name")

                if self._nopaths and save_path:
                    # 过滤不需要转移的路径
                    nopath_skip = False
                    for nopath in self._nopaths.split('\n'):
                        if os.path.normpath(save_path).startswith(os.path.normpath(nopath)):
                            logger.info(f"种子 {hash_str} 保存路径 {save_path} 不需要辅种，跳过 ...")
                            nopath_skip = True
                            break
                    if nopath_skip:
                        continue

                # 获取种子标签
                torrent_labels = self.__get_label(torrent, downloader)
                if torrent_labels and self._nolabels:
                    is_skip = False
                    for label in self._nolabels.split(','):
                        if label in torrent_labels:
                            logger.info(f"种子 {hash_str} 含有不辅种标签 {label}，跳过 ...")
                            is_skip = True
                            break
                    if is_skip:
                        continue
                hash_strs.append({
                    "hash": hash_str,
                    "save_path": save_path,
                    "torrent_info": torrent_info
                })
            if hash_strs:
                self.__seed_torrents(hash_strs=hash_strs, downloader=downloader)
                # 触发校验检查
                self.check_recheck()
            else:
                logger.info(f"没有需要辅种的种子")
        # 保存缓存
        self.__update_config()
        # 发送消息
        if self._notify:
            if self.success or self.fail:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【青蛙辅种助手辅种任务完成】",
                    text=f"服务器返回可辅种总数：{self.total}\n"
                         f"实际可辅种数：{self.realtotal}\n"
                         f"已存在：{self.exist}\n"
                         f"成功：{self.success}\n"
                         f"失败：{self.fail}\n"
                         f"{self.cached} 条失败记录已加入缓存"
                )
        logger.info("辅种任务执行完成")

    def check_recheck(self):
        """
        定时检查下载器中种子是否校验完成，校验完成且完整的自动开始辅种
        """
        if not self._recheck_torrents:
            return
        if self._is_recheck_running:
            return
        self._is_recheck_running = True
        for downloader in self._downloaders:
            # 需要检查的种子
            recheck_torrents = self._recheck_torrents.get(downloader) or []
            if not recheck_torrents:
                continue
            logger.info(f"开始检查下载器 {downloader} 的校验任务 ...")
            # 下载器
            downloader_obj = self.__get_downloader(downloader)
            # 获取下载器中的种子状态
            torrents, _ = downloader_obj.get_torrents(ids=recheck_torrents)
            if torrents:
                can_seeding_torrents = []
                for torrent in torrents:
                    # 获取种子hash
                    hash_str = self.__get_hash(torrent, downloader)
                    if self.__can_seeding(torrent, downloader):
                        can_seeding_torrents.append(hash_str)
                if can_seeding_torrents:
                    logger.info(f"共 {len(can_seeding_torrents)} 个任务校验完成，开始辅种 ...")
                    # 开始任务
                    downloader_obj.start_torrents(ids=can_seeding_torrents)
                    # 去除已经处理过的种子
                    self._recheck_torrents[downloader] = list(
                        set(recheck_torrents).difference(set(can_seeding_torrents)))
            elif torrents is None:
                logger.info(f"下载器 {downloader} 查询校验任务失败，将在下次继续查询 ...")
                continue
            else:
                logger.info(f"下载器 {downloader} 中没有需要检查的校验任务，清空待处理列表 ...")
                self._recheck_torrents[downloader] = []
        self._is_recheck_running = False

    def __seed_torrents(self, hash_strs: list, downloader: str):
        """
        执行所有种子的辅种
        """
        if not hash_strs:
            return
        logger.info(f"下载器 {downloader} 开始查询辅种，种子总数量：{len(hash_strs)} ...")

        # 每个Hash的保存目录
        save_paths = {}
        pieces_hash_set = set()
        site_pieces_hash_set = set()
        for item in hash_strs:
            tor_info: TorInfo = item.get("torrent_info")
            save_paths[tor_info.pieces_hash] = item.get("save_path")
            pieces_hash_set.add(tor_info.pieces_hash)
            if tor_info.site_name:
                site_pieces_hash_set.add(tor_info.get_name_pieces_tag())

        logger.info(f"去重后，总共需要辅种查询的种子数：{len(pieces_hash_set)}")
        pieces_hashes = list(pieces_hash_set)

        # 分站点逐个批次辅种
        # 逐个站点查询可辅种数据
        chunk_size = 100
        for site_config in self._site_cs_infos:
            db_site_info = self._name_site_map[site_config.name]
            if not db_site_info:
                logger.info(f"未在支持站点中找到{site_config.name}")
            remote_tors : list[TorInfo] = []
            total_size = len(pieces_hashes)
            for i in range(0, len(pieces_hashes), chunk_size):
                # 切片操作
                chunk = pieces_hashes[i:i + chunk_size]
                # 处理分组
                chunk_tors, err_msg = self.cross_helper.get_target_torrent(site_config,chunk)
                if not chunk_tors and err_msg:
                    logger.info(
                        f"查询站点{site_config.name}可辅种的信息出错 {err_msg},进度={i+1}/{total_size}"
                    )
                else:
                    logger.info(
                        f"站点{site_config.name}本批次的可辅种/查询数={len(chunk_tors)}/{len(chunk)},进度={i+1}/{total_size}"
                    )
                    remote_tors = remote_tors + chunk_tors

            logger.info(f"站点{site_config.name}返回可以辅种的种子总数为{len(remote_tors)}")

            # 去除已经下载过的种子
            local_cnt = 0
            not_local_tors = []
            for tor_info in remote_tors:
                if (
                    tor_info
                    and tor_info.site_name
                    and tor_info.pieces_hash
                    and tor_info.get_name_pieces_tag() in site_pieces_hash_set
                ):
                    local_cnt = local_cnt + 1
                else:
                    not_local_tors.append(tor_info)
            logger.info(f"站点{site_config.name}正在做种或已经辅种过的种子数为{local_cnt}")

            for tor_info in not_local_tors:
                if not tor_info:
                    continue
                if not tor_info.torrent_id or not tor_info.pieces_hash:
                    continue
                if tor_info.get_name_id_tag() in self._success_caches:
                    logger.info(f"{tor_info.get_name_id_tag()} 已处理过辅种，跳过 ...")
                    continue
                if tor_info.get_name_id_tag() in self._error_caches or tor_info.get_name_id_tag() in self._permanent_error_caches:
                    logger.info(f"种子 {tor_info.get_name_id_tag()} 辅种失败且已缓存，跳过 ...")
                    continue
                # 添加任务
                self.__download_torrent(tor=tor_info,site_config=site_config,site_info=db_site_info,
                                                downloader=downloader,
                                                save_path=save_paths.get(tor_info.pieces_hash))

        logger.info(f"下载器 {downloader} 辅种完成")

    def __download(self, downloader: str, content: bytes,
                   save_path: str) -> Optional[str]:
        """
        添加下载任务
        """
        if downloader == "qbittorrent":
            # 生成随机Tag
            tag = StringUtils.generate_random_str(10)
            state = self.qb.add_torrent(content=content,
                                        download_dir=save_path,
                                        is_paused=True,
                                        tag=["已整理", "辅种", tag])
            if not state:
                return None
            else:
                # 获取种子Hash
                torrent_hash = self.qb.get_torrent_id_by_tag(tags=tag)
                if not torrent_hash:
                    logger.error(f"{downloader} 下载任务添加成功，但获取任务信息失败！")
                    return None
            return torrent_hash
        elif downloader == "transmission":
            # 添加任务
            torrent = self.tr.add_torrent(content=content,
                                          download_dir=save_path,
                                          is_paused=True,
                                          labels=["已整理", "辅种"])
            if not torrent:
                return None
            else:
                return torrent.hashString

        logger.error(f"不支持的下载器：{downloader}")
        return None

    def __download_torrent(
        self,
        tor: TorInfo,
        site_config: CSSiteConfig,
        site_info: Site,
        downloader: str,
        save_path: str,
    ):
        """
        下载种子

        """
        self.total += 1
        self.realtotal += 1

        # 下载种子
        torrent_url = site_config.get_torrent_url(tor.torrent_id)

        # 下载种子文件
        _, content, _, _, error_msg = self.torrent.download_torrent(
            url=torrent_url,
            cookie=site_info.cookie,
            ua=site_info.ua or settings.USER_AGENT,
            proxy=site_info.proxy)

        # 兼容种子无法访问的情况
        if not content or (isinstance(content, bytes) and "你没有该权限".encode(encoding="utf-8") in content):
            # 下载失败
            self.fail += 1
            self.cached += 1
            # 加入失败缓存
            if error_msg and ('无法打开链接' in error_msg or '触发站点流控' in error_msg):
                self._error_caches.append(tor.get_name_id_tag())
            else:
                # 种子不存在的情况
                self._permanent_error_caches.append(tor.get_name_id_tag())
            logger.error(f"下载种子文件失败：{tor.get_name_id_tag()}")
            return False

        # 添加任务前查询校验一次，避免重复添加，导致暂停的任务被重新开始
        tmp_tor_info, err_msg = TorInfo.from_data(content)
        if tmp_tor_info and tmp_tor_info.info_hash:
            tors, msg =  self.__get_downloader(downloader).get_torrents(ids=[tmp_tor_info.info_hash])
            if tors:
                self.exist += 1
                self._success_caches.append(tor.get_name_id_tag())
                logger.info(f"下载的种子{tor.get_name_id_tag()}已存在, 跳过")
                return True
        else:
            logger.warn(f"获取下载种子的信息出错{err_msg},不能检查该种子是否已暂停")

        # 添加下载，辅种任务默认暂停
        logger.info(f"添加下载任务：{tor.get_name_id_tag()} ...")
        download_id = self.__download(downloader=downloader,
                                      content=content,
                                      save_path=save_path)
        if not download_id:
            # 下载失败
            self.fail += 1
            self.cached += 1
            # 加入失败缓存
            self._error_caches.append(tor.get_name_id_tag())
            return False
        else:
            self.success += 1
            # 追加校验任务
            logger.info(f"添加校验检查任务：{download_id} ...")
            if not self._recheck_torrents.get(downloader):
                self._recheck_torrents[downloader] = []
            self._recheck_torrents[downloader].append(download_id)
            # 下载成功
            logger.info(f"成功添加辅种下载，站点种子：{tor.get_name_id_tag()}")
            # TR会自动校验
            if downloader == "qbittorrent":
                # 开始校验种子
                self.__get_downloader(downloader).recheck_torrents(ids=[download_id])
            # 成功也加入缓存，有一些改了路径校验不通过的，手动删除后，下一次又会辅上
            self._success_caches.append(tor.get_name_id_tag())
            return True

    @staticmethod
    def __get_hash(torrent: Any, dl_type: str):
        """
        获取种子hash
        """
        try:
            return torrent.get("hash") if dl_type == "qbittorrent" else torrent.hashString
        except Exception as e:
            print(str(e))
            return ""

    @staticmethod
    def __get_label(torrent: Any, dl_type: str):
        """
        获取种子标签
        """
        try:
            return [str(tag).strip() for tag in torrent.get("tags").split(',')] \
                if dl_type == "qbittorrent" else torrent.labels or []
        except Exception as e:
            print(str(e))
            return []

    @staticmethod
    def __can_seeding(torrent: Any, dl_type: str):
        """
        判断种子是否可以做种并处于暂停状态
        """
        try:
            return torrent.get("state") == "pausedUP" if dl_type == "qbittorrent" \
                else (torrent.status.stopped and torrent.percent_done == 1)
        except Exception as e:
            print(str(e))
            return False

    @staticmethod
    def __get_save_path(torrent: Any, dl_type: str):
        """
        获取种子保存路径
        """
        try:
            return torrent.get("save_path") if dl_type == "qbittorrent" else torrent.download_dir
        except Exception as e:
            print(str(e))
            return ""

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    @eventmanager.register(EventType.SiteDeleted)
    def site_deleted(self, event):
        """
        删除对应站点选中
        """
        site_id = event.event_data.get("site_id")
        config = self.get_config()
        if config:
            sites = config.get("sites")
            if sites:
                if isinstance(sites, str):
                    sites = [sites]

                # 删除对应站点
                if site_id:
                    sites = [site for site in sites if int(site) != int(site_id)]
                else:
                    # 清空
                    sites = []

                # 若无站点，则停止
                if len(sites) == 0:
                    self._enabled = False

                self._sites = sites
                # 保存配置
                self.__update_config()
