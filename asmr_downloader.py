#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASMR 作品下载器命令行工具。

用法示例:
    python asmr_downloader.py "https://asmr.one/work/RJ01310099?path=%5B%5D#work-tree" --output D:/Downloads
    python asmr_downloader.py RJ01310099 --types audio --ext mp3,wav --output D:/Downloads
    python asmr_downloader.py RJ01310099 --exclude-ext mp4 --workers 8 --output D:/Downloads

依赖: 仅 Python 标准库（urllib/json），无需安装第三方包。
"""

import argparse
import errno
import http.client
import json
import os
import random
import re
import shutil
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API_BASE = "https://api.asmr-200.com/api"
DEFAULT_WORKERS = 8
DEFAULT_GLOBAL_WORKERS = 8  # 进程级全局下载线程上限（所有任务合计，默认 8，可配置）
DEFAULT_RETRIES = 5
CONNECT_TIMEOUT = 60  # 连接建立 + TLS 握手超时（秒）：覆盖 TCP connect / 代理 CONNECT 隧道 / SSL 握手，弱网代理下 30s 偏紧易触发 _ssl.c handshake timed out
# 默认音轨策略（无显式 --ext/--exclude-ext 时生效）：
# 音轨按去扩展名文件名归并（跨目录同曲合并），每曲只留最高优先级一个格式：
# 原声 m4a > wav/flac 转 m4a（多取 size 最大，wav 优先）> mp3（多取 size 最大）；
# 视频格式硬排除；文本/图片/其他音频保留
AUDIO_STRATEGY_EXTS = {"mp3", "wav", "flac", "m4a"}
VIDEO_EXTS = {"mp4", "mkv", "webm", "avi", "mov", "m4v", "wmv", "flv", "ts", "mts", "mpg", "mpeg"}
CHUNK_SIZE = 256 * 1024
MAX_ACTIVE_CHUNKS = 16
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) asmr-downloader/1.0"
DEFAULT_MIN_SPEED = 60.0          # 滑动窗口速度看门狗绝对下限阈值（KB/s）
DEFAULT_STALL_TIME = 10.0         # 窗口速度低于阈值持续秒数后中断连接触发重试
WATCHDOG_CHECK_BYTES = 1024 * 1024  # 每写入约 1MB 检查一次平均速率
API_RETRY_TIMES = 3               # API 请求（元数据/文件树）网络类失败重试次数
API_RETRY_BASE_DELAY = 1.0        # API 重试指数退避基数（秒）
MAX_JSON_BODY = 50 * 1024 * 1024  # API JSON 响应体上限（50MB），防内存暴涨
DISK_SPACE_FACTOR = 1.1           # 磁盘预检冗余系数：预留 1.1 倍文件大小空间

_print_lock = threading.Lock()
_state_lock = threading.Lock()
_chunk_semaphore = threading.BoundedSemaphore(MAX_ACTIVE_CHUNKS)
# 进程级全局下载并发信号量：限制所有任务的活跃下载线程总数（默认 8，可用 set_global_workers 调整）
_global_download_semaphore = threading.BoundedSemaphore(DEFAULT_GLOBAL_WORKERS)
_global_download_sem_lock = threading.Lock()


def set_global_workers(n: int) -> None:
    """调整进程级全局下载并发上限（重建信号量，对下次任务生效）。"""
    global _global_download_semaphore
    n = max(1, int(n))
    with _global_download_sem_lock:
        _global_download_semaphore = threading.BoundedSemaphore(n)

# 全局 opener（--proxy 配置后生效）；None 表示使用系统默认（系统代理/直连）
_global_opener = None
# 全局代理地址原文（连接池按 (scheme, host, port, proxy) 复用连接）
_global_proxy = ""
# Range 支持探测缓存（按 host），加锁防并发重复探测
_range_support_cache = {}
_range_support_lock = threading.Lock()
# per-host 连接池（P0-05 keep-alive 复用）
_pools: dict = {}
_pools_lock = threading.Lock()

# Windows 文件名非法字符（含控制字符）
INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Windows 保留设备名
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


_log_file = None


def set_log_file(path: str) -> None:
    """设置日志落盘文件（UTF-8）；None/空串表示仅控制台输出。"""
    global _log_file
    if path:
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        _log_file = open(Path(path).expanduser(), "a", encoding="utf-8")
    else:
        _log_file = None


def log(msg: str) -> None:
    """线程安全打印，带时间戳；设置 log 文件时同时落盘。"""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    with _print_lock:
        print(line, flush=True)
        if _log_file:
            try:
                _log_file.write(line + "\n")
                _log_file.flush()
            except OSError:
                pass


def setup_network(proxy: str = "") -> None:
    """根据 --proxy 配置全局 opener；未指定代理时保持系统代理/直连默认行为。

    仅支持 http:// 与 https:// 代理；SOCKS 代理（socks5:// 等）不受支持，忽略并警告。
    """
    global _global_opener, _global_proxy
    _global_proxy = proxy.strip() if proxy else ""
    if not proxy:
        _global_opener = None
        return
    m = re.match(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://", proxy.strip())
    scheme = m.group("scheme").lower() if m else ""
    if scheme in ("socks", "socks4", "socks5"):
        log("[代理] 警告: 不支持 SOCKS 代理，忽略 --proxy，使用系统默认网络")
        _global_opener = None
        _global_proxy = ""
        return
    if scheme not in ("http", "https"):
        raise ValueError(f"不支持的代理协议: {proxy}（仅支持 http:// 或 https://）")
    _global_opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    log(f"[代理] 已启用代理: {proxy}")


class StallError(Exception):
    """最低速度看门狗触发的中断信号：平均速率低于阈值且持续超过 stall-time，主动中断连接触发重试。"""


class RetryableHTTPError(IOError):
    """服务端瞬时错误（HTTP 408/429/5xx）：归类为可重试，由重试循环接管。"""


class APIResourceNotFoundError(IOError):
    """API 返回 HTTP 404：作品/资源确定性不存在，应快速失败，不参与网络类重试。"""


class DownloadCancelled(Exception):
    """任务取消信号：cancel_cb 返回真，或进度回调抛出取消类异常时抛出。

    传播路径：下载引擎内部检查点（分片/流式读循环）→ download_file → worker →
    download_work 的 as_completed 循环（终止等待并收尾线程池）→ 调用方（server 标记 cancelled）。
    该异常必须原样穿透所有 except 包装，禁止被当作普通下载失败处理。
    """


# 可重试的网络类异常集合：重试循环仅对这类异常重试，本地/确定性错误直接判定失败。
# 注意：本地 IO 错误（磁盘满 OSError / PermissionError / ValueError 等）不在集合内。
RETRYABLE_EXCEPTIONS = (
    urllib.error.URLError,
    TimeoutError,
    socket.timeout,
    ConnectionError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    ssl.SSLError,  # TLS 握手超时/意外 EOF（_ssl.c:989 / _ssl.c:1006）均为弱网代理抖动，应重试而非判死
    http.client.HTTPException,  # 连接池/协议层异常（BadStatusLine 等）
)


def _is_retryable(exc: BaseException) -> bool:
    """判断异常是否属于可重试的网络类错误（含看门狗/服务端瞬时错误）。"""
    return (isinstance(exc, RETRYABLE_EXCEPTIONS)
            or isinstance(exc, (StallError, RetryableHTTPError))
            or _is_timed_out_oserror(exc))


def _is_timed_out_oserror(exc: BaseException) -> bool:
    """判断是否为读超时/连接超时类普通 OSError，属于可重试网络错误。

    典型消息 "cannot read from timed out object"：底层 socket 读超时后响应对象
    已失效，再 read 抛出不带 errno 的普通 OSError，不在 RETRYABLE_EXCEPTIONS
    集合内。通过消息特征识别为可重试网络错误；磁盘满（ENOSPC）等本地 IO 错误
    不含超时消息，仍不可重试。

    同时覆盖 Windows 环境常见变体：
    - 消息含 "timeout"（"The read operation timed out" 等英文变体）
    - errno 为网络超时/连接中断类（ETIMEDOUT=110、WSAETIMEDOUT=10060、
      WSAECONNRESET=10054、WSAECONNABORTED=10053、WSAENETRESET=10052），
      这类错误消息多为中文（如 WinError 10060 连接尝试失败），不依赖字符串匹配。
    """
    if not isinstance(exc, OSError):
        return False
    msg = str(exc)
    if "timed out" in msg or "timeout" in msg:
        return True
    if exc.errno in (errno.ETIMEDOUT, 10060, 10054, 10053, 10052):
        return True
    return False


# socket 读超时轮询间隔：单次 recv 等待上限压缩到此值，超时后先查取消再继续，
# 保证任务取消无需等待最长 120/300/600s 的读超时；累计无数据超过 read_timeout 仍判 stall。
_CANCEL_POLL_INTERVAL = 5.0


def _interruptible_sleep(seconds: float, cancel_cb=None) -> None:
    """可中断分段 sleep：每 0.2s 检查一次 cancel_cb，取消立即抛 DownloadCancelled。"""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        if cancel_cb and cancel_cb():
            raise DownloadCancelled("用户取消")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))


def _sleep_backoff(attempt: int, cancel_cb=None) -> None:
    """指数退避 + 随机抖动，避免多线程同步重试风暴；支持取消中断。"""
    base = API_RETRY_BASE_DELAY * (2 ** max(0, attempt - 1))
    _interruptible_sleep(max(0.0, base + random.uniform(0, 0.5)), cancel_cb=cancel_cb)


def _ensure_disk_space(dest: Path, expected_size: int) -> None:
    """预检目标盘剩余空间是否足够容纳 expected_size（含 1.1 倍冗余），不足直接拒绝。

    无法探测磁盘空间（如相对路径异常）时静默跳过预检。
    """
    if expected_size <= 0:
        return
    try:
        anchor = dest.anchor or Path.cwd().anchor
        free_bytes = shutil.disk_usage(anchor).free
    except OSError:
        return
    need = int(expected_size * DISK_SPACE_FACTOR)
    if free_bytes < need:
        raise OSError(
            f"磁盘空间不足: 需要约 {need / (1024 * 1024):.1f} MB（文件 {expected_size / (1024 * 1024):.1f} MB），"
            f"目标盘 {anchor} 剩余 {free_bytes / (1024 * 1024):.1f} MB，拒绝下载"
        )


class _PooledConnection:
    """持有 http.client 连接的池化连接；复用前需确认底层 socket 仍存活。

    - 代理 http 请求用绝对 URI；https 走 CONNECT 隧道（set_tunnel）。
    - 连接阶段超时统一 CONNECT_TIMEOUT；读超时在 request() 中按需调整。
    """
    __slots__ = ("scheme", "host", "port", "proxy", "conn", "in_use", "dead", "pool")

    def __init__(self, pool: "ConnectionPool", scheme: str, host: str, port: int, proxy: str, timeout: int):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.proxy = proxy
        self.in_use = False
        self.dead = False
        self.pool = pool
        if proxy:
            phost, pport = _parse_proxy_endpoint(proxy)
            conn = http.client.HTTPConnection(phost, pport, timeout=timeout)
            if scheme == "https":
                conn.set_tunnel(host, port)
                # CONNECT 隧道建立后必须做 TLS 包装，否则隧道内仍是明文 HTTP，
                # 目标 443 端口会拒绝并返回 400 "plain HTTP request was sent to HTTPS port"。
                _orig_connect = conn.connect

                def _connect_with_tls():
                    _orig_connect()
                    if conn.sock is not None:
                        _ctx = ssl.create_default_context()
                        conn.sock = _ctx.wrap_socket(conn.sock, server_hostname=host)

                conn.connect = _connect_with_tls
        elif scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        self.conn = conn

    def alive(self) -> bool:
        return (not self.dead) and self.conn.sock is not None

    def request(self, method: str, target: str, headers: dict, read_timeout=None):
        """发送请求并返回响应；连接建立后按需调整底层 socket 读超时。"""
        self.conn.request(method, target, headers=headers)
        resp = self.conn.getresponse()
        if read_timeout is not None and self.conn.sock is not None:
            try:
                self.conn.sock.settimeout(read_timeout)
            except Exception:
                pass
        return resp

    def close(self) -> None:
        if not self.dead:
            try:
                self.conn.close()
            except Exception:
                pass
            self.dead = True

    def release(self, reusable: bool = True) -> None:
        """归还连接：可复用则放回池，否则关闭。异常/未读完时必须传 False。"""
        self.pool.release(self, reusable)


_POOL_MAX_IDLE = 8  # 每个 (scheme, host, port, proxy) key 最多保留的空闲连接数


class ConnectionPool:
    """per-(scheme, host, port, proxy) 连接池，线程安全：获取/归还/失效重建。"""

    def __init__(self, scheme: str, host: str, port: int, proxy: str, timeout: int):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.proxy = proxy
        self.timeout = timeout
        self._idle = []
        self._active = set()  # 正在使用中的连接（含阻塞 recv），供 force_close_connections 强制关闭
        self._lock = threading.Lock()

    def acquire(self) -> _PooledConnection:
        with self._lock:
            while self._idle:
                pc = self._idle.pop()
                if pc.alive():
                    pc.in_use = True
                    self._active.add(pc)
                    return pc
                pc.close()
        pc = _PooledConnection(self, self.scheme, self.host, self.port, self.proxy, self.timeout)
        with self._lock:
            self._active.add(pc)
        return pc

    def release(self, pc: _PooledConnection, reusable: bool) -> None:
        with self._lock:
            self._active.discard(pc)
            if len(self._idle) >= _POOL_MAX_IDLE:
                pc.close()
                return
            if pc.dead or not reusable:
                pc.close()
                return
            pc.in_use = False
            self._idle.append(pc)

    def clear(self) -> None:
        """清空空闲连接：连接/隧道异常（如代理节点 TLS 重置）后调用，避免复用坏连接。"""
        with self._lock:
            while self._idle:
                pc = self._idle.pop()
                pc.close()

    def force_close(self) -> int:
        """强制关闭本池全部连接（含正在使用），使阻塞中的 recv 立即返回；返回关闭数。"""
        with self._lock:
            items = list(self._idle) + list(self._active)
            self._idle = []
            self._active.clear()
        closed = 0
        for pc in items:
            if not pc.dead:
                pc.close()
                closed += 1
        return closed


def _parse_proxy_endpoint(proxy: str):
    """解析代理地址为 (host, port)；解析失败抛 ValueError。"""
    m = re.match(
        r"^(?P<scheme>[a-z][a-z0-9+.-]*)://(?P<host>[^/:]+)(?::(?P<port>\d+))?",
        proxy.strip(),
    )
    if not m:
        raise ValueError(f"无法解析代理地址: {proxy!r}")
    scheme = m.group("scheme").lower()
    host = m.group("host")
    port = int(m.group("port") or (443 if scheme == "https" else 8080))
    return host, port


def force_close_connections() -> int:
    """强制关闭所有连接池的全部连接（含正在使用），使阻塞中的 recv 立即抛错返回。

    用于任务取消：底层 socket 关闭后，阻塞在 recv 的下载线程会立刻收到
    timeout / ConnectionError，配合读循环中的 cancel_cb 检查点快速退出，无需等长读超时。
    返回关闭的连接总数。
    """
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    return sum(pool.force_close() for pool in pools)


def get_connection(url: str):
    """按 URL 获取池化连接，返回 (conn, urlparse 结果)。"""
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.hostname
    port = parsed.port or (443 if scheme == "https" else 80)
    proxy = _global_proxy
    key = (scheme, host, port, proxy)
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = ConnectionPool(scheme, host, port, proxy, CONNECT_TIMEOUT)
            _pools[key] = pool
    return pool.acquire(), parsed


def fetch_http(url: str, headers: dict = None, method: str = "GET",
               read_timeout: int = None):
    """发起 HTTP 请求，返回 (池化连接, HTTPResponse)。

    - 调用方读完响应体后必须 release(pc, True)；异常/未读完时 release(pc, False)。
    - 代理 http：请求行用绝对 URI；https：经 CONNECT 隧道后仍用 path。
    - 连接超时用 CONNECT_TIMEOUT（池化连接创建时生效）；读超时由 read_timeout 控制。
    """
    pc, parsed = get_connection(url)
    if pc.proxy and pc.scheme == "http":
        target = url
    else:
        target = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", UA)
    try:
        resp = pc.request(method, target, hdrs, read_timeout=read_timeout)
        return pc, resp
    except Exception:
        pc.close()
        # 连接/隧道异常（如代理节点 TLS 重置）后清空同 key 连接池，避免后续复用坏连接
        _port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        with _pools_lock:
            pool = _pools.get((parsed.scheme.lower(), parsed.hostname, _port, _global_proxy))
            if pool is not None:
                pool.clear()
        raise


def http_get_json(url: str, timeout: int = 30, retries: int = API_RETRY_TIMES) -> object:
    """GET 请求并解析 JSON。

    - 网络类异常自动重试（指数退避 + 抖动），仅重试可重试错误；
    - 响应体受 MAX_JSON_BODY 上限保护，防内存暴涨；
    - 确定性错误（内容/协议问题）直接抛出，不重试。
    """
    last_exc = None
    for attempt in range(1, retries + 1):
        reusable = False
        try:
            pc, resp = fetch_http(url, read_timeout=timeout)
            try:
                if resp.status == 404:
                    # 404 属确定性"资源不存在"（如无效 RJ 号），快速失败，不参与网络类重试
                    raise APIResourceNotFoundError(f"HTTP 404 资源不存在（RJ 号可能无效）")
                if resp.status != 200:
                    raise IOError(f"HTTP {resp.status}")
                length_header = resp.headers.get("Content-Length")
                if length_header and length_header.isdigit() and int(length_header) > MAX_JSON_BODY:
                    raise ValueError(f"响应体过大 ({length_header} B > {MAX_JSON_BODY} B)，拒绝解析")
                body = resp.read(MAX_JSON_BODY + 1)
                if len(body) > MAX_JSON_BODY:
                    raise ValueError(f"响应体超过上限 {MAX_JSON_BODY} B，拒绝解析")
                reusable = True
                return json.loads(body.decode("utf-8"))
            finally:
                pc.release(reusable)
        except ValueError as e:
            # 确定性问题（协议/内容/JSON 解析错误）：不重试，直接抛出
            raise
        except APIResourceNotFoundError:
            # 404 确定性不存在：不重试，直接抛出（由上层给出"RJ 号无效"提示）
            raise
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < retries:
                log(f"      [API重试] {url} 第 {attempt}/{retries} 次失败: {e}")
                _sleep_backoff(attempt)
    raise RuntimeError(f"API 请求失败（重试 {retries} 次）: {url}: {last_exc}")


def quote_url(url: str) -> str:
    """对 URL 中的特殊字符做安全编码，保留已存在的转义。"""
    return urllib.parse.quote(url, safe=":/?&=#%[]")


def extract_rj_number(text: str) -> str:
    """从 URL / RJ 号 / 任意文本中提取 RJ 数字串（如 RJ01310099 -> 01310099）。"""
    m = re.search(r"RJ(\d+)", text, re.IGNORECASE)
    if not m:
        raise ValueError(f"无法从输入中提取 RJ 号: {text!r}")
    return m.group(1)


def sanitize_filename(name: str) -> str:
    """清洗 Windows 非法文件名：替换非法字符、去首尾空白点、处理保留设备名。"""
    name = INVALID_CHARS.sub("_", name)
    name = name.strip(" .")
    base = name.split(".")[0].upper()
    if base in RESERVED_NAMES:
        name = "_" + name
    return name or "_"


def get_default_output_dir() -> str:
    """默认下载目录：脚本执行时所在目录（进程当前工作目录）下的 download 子目录。

    目录在下载前由 main 自动创建。
    """
    return str(Path(os.getcwd()) / "download")


def fetch_work_info(rj_number: str):
    """获取作品元数据，返回 (data, 实际使用的 rj 数字标识)。"""
    candidates = []
    for cand in (rj_number, rj_number.lstrip("0")):
        if cand and cand not in candidates:
            candidates.append(cand)
    last_err = None
    for cand in candidates:
        try:
            data = http_get_json(f"{API_BASE}/workInfo/{cand}?v2")
            if data:
                return data, cand
        except APIResourceNotFoundError as e:
            # 404 确定性"作品不存在"：换 candidate 也一样，立即快速失败并提示 RJ 号无效
            raise RuntimeError(
                f"作品不存在（HTTP 404），请检查 RJ 号 RJ{rj_number} 是否有效"
            ) from e
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"获取作品信息失败: {last_err}")


def fetch_tracks(rj_number: str):
    """获取作品文件树，返回节点列表。兼容 {tracks:[...]} 与裸数组两种结构。"""
    data = http_get_json(f"{API_BASE}/tracks/{rj_number}?v=2")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        tracks = data.get("tracks") or data.get("data") or data.get("list")
        if isinstance(tracks, list):
            return tracks
    raise RuntimeError(f"无法解析文件树结构: {type(data)}")


def extract_title(work_info: object) -> str:
    """从 workInfo 返回中尽量提取作品标题。"""
    if isinstance(work_info, dict):
        for key in ("title", "workTitle", "name"):
            if work_info.get(key):
                return str(work_info[key])
        inner = work_info.get("data") or work_info.get("work") or {}
        if isinstance(inner, dict):
            for key in ("title", "workTitle", "name"):
                if inner.get(key):
                    return str(inner[key])
    return "unknown_title"


def collect_leaves(nodes: list):
    """递归遍历文件树，收集叶子节点，返回 [(相对路径 Path, 节点 dict), ...]。

    父节点若含 children 则作为目录层；叶子节点必须有 mediaDownloadUrl。
    """
    leaves = []

    def walk(node, rel_dir: Path):
        title = node.get("title") or ""
        children = node.get("children") or []
        if children:
            sub_dir = rel_dir / sanitize_filename(title) if title else rel_dir
            for child in children:
                walk(child, sub_dir)
        elif node.get("mediaDownloadUrl"):
            leaves.append((rel_dir, node))

    for n in nodes:
        walk(n, Path(""))
    return leaves


def node_ext(node: dict) -> str:
    """叶子节点扩展名（小写，无点）。"""
    title = node.get("title") or ""
    return Path(title).suffix.lstrip(".").lower()


def filter_leaf(node: dict, types, exts, exclude_exts) -> bool:
    """按 type 字段 / 扩展名 / 排除扩展名过滤。"""
    ntype = node.get("type", "other") or "other"
    if types and ntype not in types:
        return False
    ext = node_ext(node)
    if exts and ext not in exts:
        return False
    if exclude_exts and ext in exclude_exts:
        return False
    return True


def to_m4a_url(url: str) -> str:
    """把 mediaDownloadUrl 的路径后缀改为 .m4a（保留查询参数），服务器会自动转码。"""
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path
    if "." in path.rsplit("/", 1)[-1]:
        path = path.rsplit(".", 1)[0] + ".m4a"
    else:
        path = path + ".m4a"
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
    )


def _to_m4a_node(node: dict) -> dict:
    """把 wav/flac 节点改造为 m4a 下载项（服务器自动转码）。

    - node["mediaDownloadUrl"] -> .m4a 转码流 URL
    - node["title"] -> 同名 .m4a
    - node["_m4a"] = True（worker 据此不校验 API size，按响应实际大小）
    """
    new_node = dict(node)
    stream_url = node.get("streamLowQualityUrl")
    if stream_url:
        # 源站转码流（fast.kiko /media/stream/*.m4a），已验证可用
        new_node["mediaDownloadUrl"] = stream_url
    else:
        # 兜底：改 download 路径后缀让服务器转码（raw.kiko 常返回 404，仅作兜底）
        new_node["mediaDownloadUrl"] = to_m4a_url(node["mediaDownloadUrl"])
    new_node["title"] = Path(node["title"]).stem + ".m4a"
    new_node["_m4a"] = True
    return new_node


def apply_default_audio_strategy(items: list) -> tuple:
    """默认音轨策略（作品级格式全局屏蔽）：视频硬排除；文本/图片/其他音频原样保留。

    items: [(rel_dir, node), ...]（已按 --types 过滤）
    返回 (selected, skipped_count)，selected 仍为 [(rel_dir, node), ...]。

    全局规则（不做文件名/音轨匹配）：
      - 整个作品音频计划中只要存在 m4a（含 wav/flac 可转 m4a），就全局剔除所有 mp3 下载项，
        全部音频走 m4a：原声 m4a 直下、wav/flac 转 m4a（_to_m4a_node）；
      - 仅当作品完全没有 m4a/wav/flac 时才下载 mp3。
    """
    keep = []
    audio = []  # 策略音频节点（m4a/mp3/wav/flac）
    skipped = 0
    for rel_dir, node in items:
        ext = node_ext(node)
        ntype = (node.get("type") or "other").lower()
        if ext in VIDEO_EXTS or ntype == "video":
            skipped += 1
            continue
        if ext in AUDIO_STRATEGY_EXTS:
            audio.append((rel_dir, node))
        else:
            # 文本/图片/其他音频（ogg/aac 等）保留现有下载行为
            keep.append((rel_dir, node))
    has_m4a_or_lossless = any(
        node_ext(n) in ("m4a", "wav", "flac") for _, n in audio
    )
    selected = list(keep)
    for rel_dir, node in audio:
        ext = node_ext(node)
        if ext == "m4a":
            selected.append((rel_dir, node))
        elif ext in ("wav", "flac"):
            # 有 m4a 源或可转 m4a 时，无损一律转 m4a 下载
            selected.append((rel_dir, _to_m4a_node(node)))
        else:  # mp3
            if has_m4a_or_lossless:
                skipped += 1  # 作品存在 m4a（含可转）时全局剔除 mp3
            else:
                selected.append((rel_dir, node))
    return selected, skipped


def probe_range_support(url: str) -> bool:
    """探测服务器是否支持 HTTP Range：用 Range: bytes=0-0 试探，206 即支持。按 host 缓存（带锁）。"""
    host = urllib.parse.urlparse(url).netloc
    with _range_support_lock:
        if host in _range_support_cache:
            return _range_support_cache[host]
    supported = False
    reusable = False
    try:
        pc, resp = fetch_http(quote_url(url), headers={"Range": "bytes=0-0"}, read_timeout=CONNECT_TIMEOUT)
        try:
            resp.read()  # 读掉 1 字节 body，保证连接可干净复用
            if resp.status == 206 or resp.headers.get("Accept-Ranges", "").lower() == "bytes":
                supported = True
            reusable = True
        finally:
            pc.release(reusable)
    except Exception:  # noqa: BLE001
        supported = False
    with _range_support_lock:
        _range_support_cache[host] = supported
    return supported


def probe_remote_size(url: str) -> int:
    """探测远端文件实际大小（字节）：优先 Range bytes=0-0 的 Content-Range 总长，否则用 Content-Length。

    用于 wav/flac 转 m4a 等"API size 不可信/未知"场景：分片规划、大小校验、
    断点续传 meta 均以该响应实际大小为准。探测失败返回 0。
    """
    try:
        pc, resp = fetch_http(quote_url(url), headers={"Range": "bytes=0-0"},
                              read_timeout=CONNECT_TIMEOUT)
        reusable = False
        try:
            if resp.status in (200, 206):
                if resp.status == 206:
                    cr = resp.headers.get("Content-Range", "")
                    m = re.match(r"bytes\s+0-0/(\d+)", cr)
                    resp.read(1)  # 读掉 1 字节 body，连接可干净复用
                    reusable = True
                    if m:
                        return int(m.group(1))
                    return 0
                cl = resp.headers.get("Content-Length", "")
                if cl and cl.isdigit():
                    return int(cl)
                return 0
            # 非 2xx（404/403/5xx 等）：响应体是错误页，Content-Length 不代表文件实际大小
            return 0
        finally:
            pc.release(reusable)
    except Exception:  # noqa: BLE001
        return 0


def plan_chunks(total_size: int) -> list:
    """按文件大小规划分片：目标片大小约 4MB，自适应片数（至少 1 片）。返回 [(start, end), ...]。"""
    MB = 1024 * 1024
    TARGET_CHUNK = 4 * MB
    n = max(1, (total_size + TARGET_CHUNK - 1) // TARGET_CHUNK)
    if n <= 1:
        return [(0, total_size - 1)]
    base = total_size // n
    chunks = []
    start = 0
    for i in range(n):
        end = total_size - 1 if i == n - 1 else start + base - 1
        chunks.append((start, end))
        start = end + 1
    return chunks


def load_chunk_meta(meta_path: Path):
    """读取分片状态 meta；缺失或结构不合法返回 None。"""
    try:
        with open(meta_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("chunks"), list):
            return data
    except Exception:  # noqa: BLE001
        pass
    return None


def save_chunk_meta(meta_path: Path, data: dict) -> None:
    """持久化分片状态 meta（.part.meta.json）。"""
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def read_timeout_for_size(expected_size: int) -> int:
    """按文件大小分级计算读超时（秒），避免大文件固定短超时误判。

    <50MB 用 120s；50~200MB 用 300s；>200MB 用 600s。
    """
    if expected_size > 200 * 1024 * 1024:
        return 600
    if expected_size >= 50 * 1024 * 1024:
        return 300
    return 120


def _set_read_timeout(resp, timeout: int) -> None:
    """将响应底层 socket 的读超时调整为指定值。

    urlopen 的 timeout 参数同时作用于连接与读取，连接成功后需单独调整读超时
    以适配大文件。依赖 CPython 内部实现（resp.fp.raw._sock），失败时静默忽略。
    """
    try:
        sock = resp.fp.raw._sock
    except AttributeError:
        return
    try:
        sock.settimeout(timeout)
    except Exception:  # noqa: BLE001
        pass


def format_speed(bytes_per_sec: float) -> str:
    """字节/秒格式化为可读速度（如 1.2 MB/s / 800.0 KB/s）。"""
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
    return f"{bytes_per_sec / 1024:.1f} KB/s"


def _enforce_min_speed(total_bytes: int, conn_start: float, stall_started: float | None,
                       min_speed_bps: float, stall_time: float, now: float) -> float | None:
    """最低速度看门狗单次检查：平均速率低于阈值且持续超过 stall_time 时抛 StallError。

    - 平均速率 = 累计字节 / 连接开始后耗时（覆盖“连接建立后长时间无任何数据”的窗口）。
    - 速率恢复后重置 stall 计时。
    """
    if conn_start <= 0:
        return stall_started
    avg_bps = total_bytes / max(now - conn_start, 1e-9)
    if avg_bps < min_speed_bps:
        if stall_started is None:
            stall_started = now
        elif now - stall_started >= stall_time:
            raise StallError(
                f"下载速度过低 {avg_bps / 1024:.1f} KB/s（阈值 {min_speed_bps / 1024:.1f} KB/s），"
                f"持续 {now - stall_started:.0f}s >= {stall_time:.0f}s，主动中断连接触发重试"
            )
    else:
        stall_started = None
    return stall_started


def download_file(url: str, dest: Path, expected_size: int, retries: int,
                  min_speed_kbps: float = DEFAULT_MIN_SPEED,
                  stall_time: float = DEFAULT_STALL_TIME,
                  preallocate: bool = True, cancel_cb=None) -> tuple:
    """下载单个文件：支持 Range 时走分片断点续传，否则退化整文件流式下载。

    expected_size <= 0 时（如 wav/flac 转 m4a，API size 是源文件大小不可信），
    先探测服务端实际大小，再按实际大小规划分片 / 校验 / 续传。
    cancel_cb() 可选取消回调：返回真时抛 DownloadCancelled，终止下载（探测/分片/流式内部检查点）。
    返回 (ok: bool, reason: str)，失败时 reason 为具体失败原因（HTTP 状态码 / 异常消息等）。
    """
    part_path = dest.with_name(dest.name + ".part")
    meta_path = dest.with_name(dest.name + ".part.meta.json")
    actual_size = expected_size
    if expected_size <= 0:
        # 探测可能因 CDN/代理瞬时抖动返回失败，重试最多 3 次
        for probe_attempt in range(3):
            if cancel_cb and cancel_cb():
                raise DownloadCancelled("用户取消")
            actual_size = probe_remote_size(url)
            if actual_size > 0:
                break
            if probe_attempt < 2:
                _interruptible_sleep(1.0, cancel_cb)
        if actual_size > 0:
            log(f"    [探测] {dest.name}: 服务端实际大小 {actual_size} B（API size 不可信，以响应为准）")
    read_timeout = read_timeout_for_size(max(actual_size, expected_size))
    _ensure_disk_space(dest, max(actual_size, expected_size))
    if cancel_cb and cancel_cb():
        raise DownloadCancelled("用户取消")
    if actual_size > 0 and probe_range_support(url):
        log(f"    [Range] {dest.name}: 分片下载（{len(plan_chunks(actual_size))} 片，总 {actual_size} B）")
        return download_file_range(url, part_path, meta_path, dest, actual_size, retries, read_timeout, min_speed_kbps, stall_time, preallocate, cancel_cb=cancel_cb)
    if meta_path.exists():
        # 服务器不支持 Range：清理可能残留的分片 meta，走整文件下载
        meta_path.unlink()
    log(f"    [整文件] {dest.name}: 服务器不支持 Range，流式下载")
    return download_file_whole(url, part_path, dest, actual_size, retries, read_timeout, min_speed_kbps, stall_time, cancel_cb=cancel_cb)


def download_file_whole(url: str, part_path: Path, dest: Path, expected_size: int, retries: int,
                        read_timeout: int,
                        min_speed_kbps: float = DEFAULT_MIN_SPEED,
                        stall_time: float = DEFAULT_STALL_TIME,
                        cancel_cb=None) -> tuple:
    """整文件流式下载（Range 退化路径）：先写 .part，成功后原子改名。

    不启用最低速度看门狗：整文件无断点续传，30s 无新数据在弱网/代理下是常见抖动，
    判死重试会反复浪费整个文件（日志中 3 次失败后第二次运行秒成功即为间歇抖动）；
    改为分级读超时（read_timeout 120/300/600s）兜底，超时按可重试网络错误处理。
    min_speed_kbps / stall_time 参数仅为兼容调用签名保留，流式模式不使用。
    cancel_cb() 可选取消回调：每读一块后检查，返回真时抛 DownloadCancelled。

    返回 (ok: bool, reason: str)，失败时 reason 为最后一次失败的异常消息。
    """
    last_error = ""
    for attempt in range(1, retries + 1):
        if cancel_cb and cancel_cb():
            raise DownloadCancelled("用户取消")
        try:
            if part_path.exists():
                part_path.unlink()
            # 单次 recv 等待上限压缩到 _CANCEL_POLL_INTERVAL：超时后先查取消再继续，
            # 取消无需等最长 read_timeout；累计无数据超过 read_timeout 仍判 stall 重试。
            pc, resp = fetch_http(quote_url(url), read_timeout=min(read_timeout, _CANCEL_POLL_INTERVAL))
            reusable = False
            try:
                if resp.status != 200:
                    # 404/403 多为 CDN 并发限流/回源失败瞬时返回，同 408/429/5xx 一并可重试
                    if resp.status in (404, 403) or resp.status in (408, 429) or resp.status >= 500:
                        raise RetryableHTTPError(f"服务端瞬时错误 HTTP {resp.status}")
                    raise IOError(f"HTTP {resp.status}")
                conn_start = time.monotonic()
                total_bytes = 0
                last_data_at = time.monotonic()
                with open(part_path, "wb") as fh:
                    while True:
                        if cancel_cb and cancel_cb():
                            raise DownloadCancelled("用户取消")
                        try:
                            chunk = resp.read(CHUNK_SIZE)
                        except (TimeoutError, socket.timeout) as e:
                            if cancel_cb and cancel_cb():
                                raise DownloadCancelled("用户取消") from e
                            if time.monotonic() - last_data_at >= read_timeout:
                                raise StallError(
                                    f"连续无新数据超过 {read_timeout:.0f}s（读超时看门狗）: {part_path.name}"
                                ) from e
                            continue
                        if not chunk:
                            break
                        fh.write(chunk)
                        total_bytes += len(chunk)
                        last_data_at = time.monotonic()
                reusable = True  # body 已读尽，连接可复用
                elapsed = time.monotonic() - conn_start
                if elapsed > 0:
                    log(f"    [完成] {dest.name} 平均速度 {format_speed(total_bytes / elapsed)}")
            finally:
                pc.release(reusable)
            if expected_size and part_path.stat().st_size != expected_size:
                raise IOError(
                    f"大小不符: 期望 {expected_size} B, 实际 {part_path.stat().st_size} B"
                )
            part_path.replace(dest)
            return True, ""
        except DownloadCancelled:
            raise
        except StallError as e:
            last_error = f"{type(e).__name__}: {e}"
            log(f"    [重试] {dest.name} 第 {attempt}/{retries} 次失败(看门狗): {e}")
            if attempt < retries:
                _sleep_backoff(attempt, cancel_cb)
        except RetryableHTTPError as e:
            last_error = f"{type(e).__name__}: {e}"
            log(f"    [重试] {dest.name} 第 {attempt}/{retries} 次失败(HTTP {e}): {e}")
            if attempt < retries:
                _sleep_backoff(attempt, cancel_cb)
        except RETRYABLE_EXCEPTIONS as e:
            last_error = f"{type(e).__name__}: {e}"
            log(f"    [重试] {dest.name} 第 {attempt}/{retries} 次失败: {e}")
            if attempt < retries:
                _sleep_backoff(attempt, cancel_cb)
        except OSError as e:
            if _is_timed_out_oserror(e):
                # http.client 读超时后再次 read 的普通 OSError：视为可重试网络错误
                if attempt < retries:
                    last_error = f"{type(e).__name__}: {e}"
                    log(f"    [重试] {dest.name} 第 {attempt}/{retries} 次失败(读超时): {e}")
                    _sleep_backoff(attempt, cancel_cb)
                else:
                    # 连续重试全部读超时：给明确失败原因，不再裸抛 OSError
                    last_error = f"连续 {retries} 次重试失败（读超时）: {type(e).__name__}: {e}"
                    log(f"    [失败] {dest.name} 连续 {retries} 次重试失败（读超时）: {e}")
            else:
                # 磁盘满等本地 IO 错误仍不可重试
                last_error = f"{type(e).__name__}: {e}"
                log(f"    [失败] {dest.name} 不可重试错误(OSError): {e}")
                return False, last_error
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            log(f"    [失败] {dest.name} 不可重试错误({type(e).__name__}): {e}")
            return False, last_error
    return False, last_error


def download_chunk(url: str, part_path: Path, start: int, end: int, read_timeout: int, retries: int,
                   min_speed_kbps: float = DEFAULT_MIN_SPEED,
                   stall_time: float = DEFAULT_STALL_TIME,
                   cancel_cb=None) -> tuple:
    """用 Range 下载一个分片到 .part 对应偏移；分片内部重试，耗尽后返回 False。

    跨重试断点续传：offset 维护已成功写入的字节数，每次 attempt 从 start+offset
    请求 Range，只补下剩余部分，避免重试重下已写入的数据。
    速度看门狗：最近 8s 滑动窗口速度低于 max(60KB/s, 峰值×50%) 且持续超过
    stall_time 时抛 StallError 触发重试；连续无数据（读超时）仍走 StallError。
    受全局信号量约束，控制总活跃分片数，避免与文件级并发叠加导致连接爆炸。
    cancel_cb() 可选取消回调：每读一块后检查，返回真时抛 DownloadCancelled。
    返回 (ok: bool, reason: str)，失败时 reason 为最后一次失败的异常消息。
    """
    with _chunk_semaphore:
        last_error = ""
        offset = 0  # 已成功写入本分片的字节数（跨 attempt 累计，实现断点续传）
        for attempt in range(1, retries + 1):
            req_start = start + offset
            if req_start > end:
                break
            if cancel_cb and cancel_cb():
                raise DownloadCancelled("用户取消")
            try:
                pc, resp = fetch_http(quote_url(url), headers={"Range": f"bytes={req_start}-{end}"},
                                      read_timeout=min(read_timeout, stall_time, _CANCEL_POLL_INTERVAL))
                reusable = False
                try:
                    if resp.status != 206:
                        # 404/403 多为 CDN 并发限流/回源失败瞬时返回，同 408/429/5xx 一并可重试
                        if resp.status in (404, 403) or resp.status in (408, 429) or resp.status >= 500:
                            raise RetryableHTTPError(f"服务端瞬时错误 HTTP {resp.status}")
                        raise IOError(f"服务器忽略 Range（HTTP {resp.status}）")
                    rstart = start
                    cr = resp.headers.get("Content-Range", "")
                    m = re.match(r"bytes\s+(\d+)-(\d+)/", cr)
                    if m:
                        rstart = int(m.group(1))
                    if rstart != req_start:
                        raise IOError(f"Range 响应偏移不符: 请求 {req_start}, 响应 {rstart}")
                    conn_start = time.monotonic()
                    total_bytes = 0
                    last_data_at = time.monotonic()
                    # 滑动窗口速度看门狗：记录 (时间戳, 累计字节)，窗口速度 = (当前字节-窗口起点字节)/窗口时长
                    window = deque()
                    peak_speed = 0.0   # 本连接窗口速度历史峰值（B/s）
                    slow_since = None  # 窗口速度持续低于阈值的起始时间
                    window_secs = 8.0
                    # 读块随 stall_time×min_speed 收缩，保证低速率下检查频率足够（滴流也能按时中断）
                    read_block = min(CHUNK_SIZE, max(32 * 1024, int(stall_time * min_speed_kbps * 1024 / 4)))
                    with open(part_path, "r+b") as fh:
                        fh.seek(req_start)
                        remaining = end - req_start + 1
                        while remaining > 0:
                            if cancel_cb and cancel_cb():
                                raise DownloadCancelled("用户取消")
                            try:
                                chunk = resp.read(min(read_block, remaining))
                            except (TimeoutError, socket.timeout) as e:
                                # 单次 recv 超时（上限 _CANCEL_POLL_INTERVAL）：先查取消，未取消则继续；
                                # 连续无数据累计超过 read_timeout 才判 stall 主动中断重试
                                if cancel_cb and cancel_cb():
                                    raise DownloadCancelled("用户取消") from e
                                if time.monotonic() - last_data_at >= read_timeout:
                                    raise StallError(
                                        f"连续无新数据超过 {read_timeout:.0f}s（读超时看门狗）: "
                                        f"{part_path.name} [{start}-{end}]"
                                    ) from e
                                continue
                            if not chunk:
                                raise IOError(
                                    f"分片提前结束（连接中断）: {part_path.name} [{start}-{end}]"
                                )
                            fh.write(chunk)
                            total_bytes += len(chunk)
                            offset += len(chunk)  # 断点续传：字节落地即推进 offset，失败重试从 start+offset 继续
                            remaining -= len(chunk)
                            now = time.monotonic()
                            last_data_at = now
                            window.append((now, total_bytes))
                            while len(window) >= 2 and now - window[0][0] > window_secs:
                                window.popleft()
                            if now - window[0][0] >= window_secs:
                                span = now - window[0][0]
                                win_speed = (total_bytes - window[0][1]) / span
                                peak_speed = max(peak_speed, win_speed)
                                threshold = max(60 * 1024, peak_speed * 0.5)
                                if win_speed < threshold:
                                    if slow_since is None:
                                        slow_since = now
                                    elif now - slow_since >= stall_time:
                                        raise StallError(
                                            f"窗口速度 {win_speed / 1024:.1f} KB/s 低于阈值 "
                                            f"{threshold / 1024:.1f} KB/s（峰值 {peak_speed / 1024:.1f} KB/s×25%），"
                                            f"持续 {now - slow_since:.0f}s >= {stall_time:.0f}s（滑动窗口看门狗）: "
                                            f"{part_path.name} [{start}-{end}]"
                                        )
                                else:
                                    slow_since = None
                    reusable = True  # body 已读尽，连接可复用
                    elapsed = time.monotonic() - conn_start
                    if elapsed > 0:
                        log(f"      [分片完成] {part_path.name} [{start}-{end}] 平均速度 {format_speed(total_bytes / elapsed)}")
                finally:
                    pc.release(reusable)
                return True, ""
            except DownloadCancelled:
                raise
            except StallError as e:
                last_error = f"{type(e).__name__}: {e}"
                log(f"      [分片重试] {part_path.name} [{start}-{end}] 第 {attempt}/{retries} 次失败(看门狗): {e}")
                if attempt < retries:
                    _sleep_backoff(attempt, cancel_cb)
            except RetryableHTTPError as e:
                last_error = f"{type(e).__name__}: {e}"
                log(f"      [分片重试] {part_path.name} [{start}-{end}] 第 {attempt}/{retries} 次失败(HTTP {e}): {e}")
                if attempt < retries:
                    _sleep_backoff(attempt, cancel_cb)
            except RETRYABLE_EXCEPTIONS as e:
                last_error = f"{type(e).__name__}: {e}"
                log(f"      [分片重试] {part_path.name} [{start}-{end}] 第 {attempt}/{retries} 次失败: {e}")
                if attempt < retries:
                    _sleep_backoff(attempt, cancel_cb)
            except OSError as e:
                if _is_timed_out_oserror(e):
                    # http.client 读超时后再次 read 的普通 OSError：视为可重试网络错误，
                    # offset 已累计已写入字节，重试从 start+offset 继续（断点续传）
                    if attempt < retries:
                        last_error = f"{type(e).__name__}: {e}"
                        log(f"      [分片重试] {part_path.name} [{start}-{end}] 第 {attempt}/{retries} 次失败(读超时): {e}")
                        _sleep_backoff(attempt, cancel_cb)
                    else:
                        # 连续重试全部读超时：给明确失败原因，不再裸抛 OSError
                        last_error = f"连续 {retries} 次重试失败（读超时）: {type(e).__name__}: {e}"
                        log(f"      [分片失败] {part_path.name} [{start}-{end}] 连续 {retries} 次重试失败（读超时）: {e}")
                else:
                    # 磁盘满等本地 IO 错误仍不可重试
                    last_error = f"{type(e).__name__}: {e}"
                    log(f"      [分片失败] {part_path.name} [{start}-{end}] 不可重试错误(OSError): {e}")
                    return False, last_error
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                log(f"      [分片失败] {part_path.name} [{start}-{end}] 不可重试错误({type(e).__name__}): {e}")
                return False, last_error
    return False, last_error


def download_file_range(url: str, part_path: Path, meta_path: Path, dest: Path, total_size: int, retries: int,
                        read_timeout: int,
                        min_speed_kbps: float = DEFAULT_MIN_SPEED,
                        stall_time: float = DEFAULT_STALL_TIME,
                        preallocate: bool = True, cancel_cb=None) -> tuple:
    """Range 分片下载 + 分片级断点续传。

    - .part 按偏移写入各分片；完成状态持久化到 .part.meta.json。
    - 入口自愈：meta 无效、或 .part 缺失 / 大小与预期不符（预分配模式）时，统一重置分片状态。
    - 重试时仅重下未完成分片，已完成分片跳过。
    - 全部片完成后校验总大小，原子改名去掉 .part，删除 meta。
    - 任一单片重试耗尽则文件标记失败，但保留已完成的片与 meta 供下次续传。
    - preallocate=False 时不做全量预分配，分片按偏移写入时自动扩展（稀疏式边下边写）。
    - cancel_cb() 可选取消回调：每片完成后 / 下一批提交前检查，返回真时抛 DownloadCancelled。
    """
    chunks = plan_chunks(total_size)
    meta = load_chunk_meta(meta_path)
    meta_preallocate = meta.get("preallocate", True) if meta else True
    size_mismatch = (
        meta is not None and meta_preallocate
        and part_path.exists() and part_path.stat().st_size != total_size
    )
    reset_required = (
        meta is None
        or meta.get("total_size") != total_size
        or len(meta.get("chunks", [])) != len(chunks)
        or not part_path.exists()
        or size_mismatch
    )
    if reset_required:
        # 全新 / 规划变化 / 磁盘不一致：重置 part 与分片状态
        chunk_states = [
            {"start": s, "end": e, "completed": False}
            for (s, e) in chunks
        ]
        if preallocate:
            with open(part_path, "wb") as fh:
                fh.truncate(total_size)
        else:
            # 不预分配：仅创建空文件，各分片按偏移写入时自动扩展
            with open(part_path, "wb"):
                pass
        meta = {"total_size": total_size, "chunks": chunk_states, "preallocate": preallocate}
        save_chunk_meta(meta_path, meta)
    chunk_states = meta["chunks"]

    if cancel_cb and cancel_cb():
        raise DownloadCancelled("用户取消")
    failed_any = False
    last_fail_reason = ""
    pending = [c for c in chunk_states if not c["completed"]]
    # 线程池在文件级复用（P2-18）：跨轮重试不再反复创建/销毁线程
    pool = ThreadPoolExecutor(max_workers=min(4, max(1, len(chunks))))
    try:
        while pending and not failed_any:
            if cancel_cb and cancel_cb():
                raise DownloadCancelled("用户取消")
            futures = {
                pool.submit(download_chunk, url, part_path, c["start"], c["end"], read_timeout, retries, min_speed_kbps, stall_time, cancel_cb): c
                for c in pending
            }
            for fut in as_completed(futures):
                c = futures[fut]
                try:
                    ok, reason = fut.result(timeout=30)
                except DownloadCancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    log(f"      [分片线程异常] {part_path.name} [{c['start']}-{c['end']}]: {e}")
                    ok, reason = False, f"分片线程异常: {e}"
                if ok:
                    c["completed"] = True
                else:
                    failed_any = True
                    last_fail_reason = reason or f"分片 [{c['start']}-{c['end']}] 下载失败"
            save_chunk_meta(meta_path, meta)
            if cancel_cb and cancel_cb():
                raise DownloadCancelled("用户取消")
            pending = [c for c in chunk_states if not c["completed"]]
    finally:
        # 收尾：wait=True 确保分片线程真正结束（已运行分片靠 cancel_cb 读循环检查点
        # 退出，不会无限等待）；未启动分片 future 由 cancel_futures 丢弃。
        pool.shutdown(wait=True, cancel_futures=True)

    if cancel_cb and cancel_cb():
        raise DownloadCancelled("用户取消")
    if failed_any:
        log(f"    [分片失败] {part_path.name}: 存在重试耗尽的分片，保留 .part 与 .meta 供下次续传")
        return False, last_fail_reason
    if part_path.stat().st_size != total_size:
        log(f"    [分片失败] {part_path.name}: 大小校验失败 {part_path.stat().st_size} != {total_size}")
        return False, f"大小校验失败: 期望 {total_size} B, 实际 {part_path.stat().st_size} B"
    part_path.replace(dest)
    meta_path.unlink()
    return True, ""


class Progress:
    """并发下载进度统计（线程安全）。"""

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.ok = 0
        self.skipped = 0
        self.failed = 0

    def tick(self, status: str) -> None:
        with _state_lock:
            self.done += 1
            if status == "ok":
                self.ok += 1
            elif status == "skip":
                self.skipped += 1
            else:
                self.failed += 1
            done, total = self.done, self.total
        log(f"  [进度] {done}/{total} ({(done / total * 100):.1f}%) 成功={self.ok} 跳过={self.skipped} 失败={self.failed}")


def positive_int(value: str) -> int:
    """argparse 校验：正整数。"""
    iv = int(value)
    if iv <= 0:
        raise argparse.ArgumentTypeError(f"必须是正整数（当前 {value}）")
    return iv


def positive_float(value: str) -> float:
    """argparse 校验：正浮点数。"""
    fv = float(value)
    if fv <= 0:
        raise argparse.ArgumentTypeError(f"必须是正数（当前 {value}）")
    return fv


def _pick_cover_url(work_info: object) -> str:
    """从 workInfo 的 images 字段中提取一张可用的封面 URL（兼容 dict/list/str 结构）。"""
    if not isinstance(work_info, dict):
        return ""
    images = work_info.get("images")
    if isinstance(images, dict):
        for key in ("main", "thumbnail", "sample", "small", "large"):
            url = images.get(key)
            if isinstance(url, str) and url.startswith("http"):
                return url
        for value in images.values():
            if isinstance(value, str) and value.startswith("http"):
                return value
    elif isinstance(images, list):
        for item in images:
            if isinstance(item, str) and item.startswith("http"):
                return item
            if isinstance(item, dict):
                for key in ("url", "main", "thumbnail", "small"):
                    url = item.get(key)
                    if isinstance(url, str) and url.startswith("http"):
                        return url
    elif isinstance(images, str) and images.startswith("http"):
        return images
    # API v2 workInfo 无 images 字段时，回退到封面直链字段（main/thumbnail/sam）
    for key in ("mainCoverUrl", "thumbnailCoverUrl", "samCoverUrl"):
        url = work_info.get(key)
        if isinstance(url, str) and url.startswith("http"):
            return url
    return ""


def _download_cover(url: str, dest: Path, timeout: int = 60) -> bool:
    """下载封面图片到 dest；失败返回 False（不抛异常，避免阻塞下载流程）。"""
    try:
        pc, resp = fetch_http(url, read_timeout=timeout)
        try:
            if resp.status != 200:
                return False
            body = resp.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                return False
            dest.write_bytes(body)
            return True
        finally:
            pc.release(resp.status == 200)
    except Exception:  # noqa: BLE001
        return False


def _save_meta_and_cover(out_dir: Path, work_info: object) -> None:
    """下载开始阶段：把完整 meta 落盘为 meta.json（UTF-8），并尽力下载封面 cover.jpg。

    - meta.json 写入失败仅告警，不阻塞下载；
    - 封面下载失败同样不阻塞（作品库展示时 cover 缺失回退占位图）。
    """
    try:
        meta_path = out_dir / "meta.json"
        meta_path.write_text(
            json.dumps(work_info, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"    [meta] 已保存 meta.json")
    except Exception as e:  # noqa: BLE001
        log(f"    [meta] 写入 meta.json 失败（不阻塞）: {e}")
    cover_url = _pick_cover_url(work_info)
    if cover_url:
        cover_dest = out_dir / "cover.jpg"
        if not cover_dest.exists():
            if _download_cover(cover_url, cover_dest):
                log(f"    [封面] 已保存: {cover_dest.name}")
            else:
                log("    [封面] 下载失败（忽略，不影响下载）")


MANIFEST_VERSION = 1
MANIFEST_FILENAME = "manifest.json"


def _save_manifest(out_dir: Path, rj: str, title: str, selected: list) -> Path:
    """把下载计划（文件相对路径 + URL + 预期大小）持久化为作品目录 manifest.json。

    复用续传时跳过 workInfo/tracks API；m4a 转码项记录原始 size，下载时以 _m4a
    标志忽略大小校验（与 worker 现有逻辑一致）。写入失败仅告警，不阻塞下载。
    """
    files = []
    for rel_dir, node in selected:
        rel = rel_dir.as_posix() if isinstance(rel_dir, Path) else str(rel_dir)
        files.append({
            "rel_dir": rel,
            "title": node.get("title") or "untitled",
            "url": node.get("mediaDownloadUrl", ""),
            "size": int(node.get("size") or 0),
            "m4a": bool(node.get("_m4a", False)),
        })
    manifest = {
        "version": MANIFEST_VERSION,
        "rj": rj,
        "title": title,
        "files": files,
    }
    path = out_dir / MANIFEST_FILENAME
    try:
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"    [manifest] 已保存下载计划 {MANIFEST_FILENAME}（{len(files)} 个文件）")
    except Exception as e:  # noqa: BLE001
        log(f"    [manifest] 写入 {MANIFEST_FILENAME} 失败（不阻塞）: {e}")
    return path


def _load_manifest(output_base: str, rj_digits: str):
    """在输出根目录下查找 RJ 对应作品目录的 manifest.json，返回 (manifest, out_dir)。

    目录名格式 RJ{digits}_{title}（与下载时 folder_name 一致）；多个候选时取目录
    修改时间最新者，避免旧版本干扰。manifest 无效（version/rj/files 缺失或不符）
    时返回 (None, None)。
    """
    try:
        base = Path(output_base)
        candidates = []
        for d in base.glob(f"RJ{rj_digits}_*"):
            if not d.is_dir():
                continue
            mp = d / MANIFEST_FILENAME
            if not mp.is_file():
                continue
            try:
                data = json.loads(mp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if (isinstance(data, dict) and data.get("version") == MANIFEST_VERSION
                    and data.get("rj") == f"RJ{rj_digits}"
                    and isinstance(data.get("files"), list)):
                candidates.append((d.stat().st_mtime, data, d))
        if not candidates:
            return None, None
        _, data, d = max(candidates, key=lambda t: t[0])
        return data, d
    except Exception:  # noqa: BLE001
        return None, None


def _selected_from_manifest(manifest: dict) -> list:
    """把 manifest 文件条目还原为 (rel_dir, node) 下载列表（与解析流程同构）。"""
    selected = []
    for f in manifest.get("files") or []:
        rel_dir = f.get("rel_dir") or ""
        node = {
            "title": f.get("title") or "untitled",
            "mediaDownloadUrl": f.get("url", ""),
            "size": int(f.get("size") or 0),
            "_m4a": bool(f.get("m4a", False)),
        }
        selected.append((Path(rel_dir) if rel_dir else Path(""), node))
    return selected


def download_work(rj_input: str, output_dir: str = "", types: str = "", exts: str = "",
                  exclude_exts: str = "", include_regex: str = "", exclude_regex: str = "",
                  workers: int = DEFAULT_WORKERS, retries: int = DEFAULT_RETRIES,
                  min_speed: float = DEFAULT_MIN_SPEED, stall_time: float = DEFAULT_STALL_TIME,
                  preallocate: bool = True, on_progress=None, cancel_cb=None) -> dict:
    """下载单个作品完整流程：元数据 → 文件树 → 过滤 → 并发下载。

    CLI 与 server 共用入口（模块化重构核心）。保持 CLI 语义向后兼容。
    on_progress(stage, data) 可选进度回调：
      - stage "meta":   {"rj", "title"}
      - stage "tree":   {"leaves"}
      - stage "filter": {"selected", "skipped"}
      - stage "file":   {"index", "total", "path", "ok", "skip"(可选)}
      - stage "done":   {"stats"}
    cancel_cb() 可选取消回调：返回真时抛 DownloadCancelled 终止下载（回调需线程安全）。
    取消信号在 worker 文件前后、进度回调前、分片每片完成后、流式读循环中周期检查。
    返回 stats dict：{rj, title, out_dir, total, success, skip, fail, failed_files,
                      error(可选)}
    取消时抛 DownloadCancelled（由调用方决定如何标记），不返回 stats。
    """
    stats = {"rj": "", "title": "", "out_dir": "", "total": 0, "success": 0,
             "skip": 0, "fail": 0, "failed_files": []}

    def _check_cancel() -> None:
        """取消检查点：cancel_cb 返回真时抛 DownloadCancelled。"""
        if cancel_cb and cancel_cb():
            raise DownloadCancelled("用户取消")

    def _emit_progress(stage: str, data: dict) -> None:
        """进度回调统一出口：回调前检查取消；回调内异常统一转抛 DownloadCancelled。"""
        _check_cancel()
        if on_progress:
            try:
                on_progress(stage, data)
            except Exception as e:  # noqa: BLE001
                raise DownloadCancelled("用户取消") from e

    def _use_manifest(manifest_data: dict, manifest_out_dir: Path, *, via_api_fallback: bool):
        """从本地 manifest 恢复下载计划：构造待下载列表并判断是否全部完成。

        返回 (pending, out_dir)；pending 为待续传的 [(rel_dir, node), ...]，
        全部完成时 pending 为 None（此时已汇报 done 并可直接 return stats）。
        """
        title = manifest_data.get("title") or "unknown_title"
        stats["title"] = title
        _emit_progress("meta", {"rj": stats["rj"], "title": title})
        out_dir = manifest_out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        stats["out_dir"] = str(out_dir)
        if via_api_fallback:
            log(f"[2/4] 改用本地 manifest 兜底续传: {out_dir / MANIFEST_FILENAME}")
        else:
            log(f"[2/4] 复用本地 manifest（跳过 workInfo API）: {out_dir / MANIFEST_FILENAME}")
        log(f"      作品: {title}")
        selected = _selected_from_manifest(manifest_data)
        pending = []
        for rel_dir, node in selected:
            dest = out_dir / rel_dir / sanitize_filename(node.get("title") or "untitled")
            expected = 0 if node.get("_m4a") else int(node.get("size") or 0)
            if not (dest.exists() and dest.stat().st_size > 0
                    and (not expected or dest.stat().st_size == expected)):
                pending.append((rel_dir, node))
        if not pending:
            log("本地 manifest 文件均已下载完成，无需续传。")
            stats["total"] = len(selected)
            stats["skip"] = len(selected)
            if on_progress:
                _emit_progress("done", {"stats": stats})
            return None, out_dir
        log(f"[3/4] manifest 文件列表 {len(selected)} 个，待续传 {len(pending)} 个")
        # total 用 manifest 计划总数：Web 端文件总数/进度按作品维度显示，而非本次续传数量
        stats["total"] = len(selected)
        return pending, out_dir

    def _run_downloads(selected) -> dict:
        """并发下载 selected（统一执行入口，原流程与 manifest 复用共用）。

        依赖外层 out_dir / workers / retries / min_speed / stall_time / preallocate
        / cancel_cb / _check_cancel / _emit_progress（调用前必须已就绪）。
        """
        # total 优先沿用外层已设置的（manifest 续传=作品计划总数），正常流程才按 selected 计
        stats["total"] = stats.get("total") or len(selected)
        log(f"[4/4] 开始下载（{workers} 线程，重试 {retries} 次）...")
        progress = Progress(len(selected))
        used_dest_names = set()  # P1-07: 同目录内文件名冲突唯一化

        def worker(item):
            rel_dir, node = item
            _check_cancel()
            dest = out_dir / rel_dir / sanitize_filename(node.get("title") or "untitled")
            if dest.name in used_dest_names:
                stem, suffix = dest.stem, dest.suffix
                i = 1
                while True:
                    cand = dest.with_name(f"{stem}_{i}{suffix}")
                    if cand.name not in used_dest_names and not cand.exists():
                        dest = cand
                        break
                    i += 1
            used_dest_names.add(dest.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # m4a 转换项：API size 是源文件（wav/flac）大小，不可信 → expected=0，
            # 大小以响应 Content-Length 为准；"已存在跳过"按目标 .m4a 存在且 size>0 判断
            expected = 0 if node.get("_m4a") else int(node.get("size") or 0)
            if dest.exists() and dest.stat().st_size > 0 and (not expected or dest.stat().st_size == expected):
                progress.tick("skip")
                if on_progress:
                    _emit_progress("file", {"index": progress.done, "total": stats["total"],
                                         "path": str(dest), "ok": True, "skip": True})
                return None
            with _global_download_semaphore:
                ok, reason = download_file(node["mediaDownloadUrl"], dest, expected, retries, min_speed,
                                           stall_time, preallocate=preallocate, cancel_cb=cancel_cb)
            _check_cancel()
            progress.tick("ok" if ok else "fail")
            if on_progress:
                _emit_progress("file", {"index": progress.done, "total": stats["total"],
                                     "path": str(dest), "ok": ok})
            if not ok:
                return {"path": str(dest), "reason": reason or "下载失败"}
            return None

        pool = ThreadPoolExecutor(max_workers=workers)
        failed_files = []
        try:
            futures = [pool.submit(worker, item) for item in selected]
            try:
                for fut in as_completed(futures):
                    try:
                        err = fut.result(timeout=30)
                    except DownloadCancelled:
                        raise
                    except Exception as e:  # noqa: BLE001
                        err = f"下载线程异常: {e}"
                    if err:
                        failed_files.append(err)
            except DownloadCancelled:
                log("[取消] 收到取消信号，终止等待剩余文件线程")
                raise
        finally:
            # 收尾：wait=True 确保线程真正结束（已运行线程靠 cancel_cb 检查点退出，
            # 不会无限等待）；未启动 future 由 cancel_futures 丢弃；取消后剩余的
            # 已运行 future 仍会被逐个 result(timeout=30) 兜底，保证不卡住。
            pool.shutdown(wait=True, cancel_futures=True)

        stats["success"] = progress.ok
        stats["skip"] = progress.skipped
        stats["fail"] = len(failed_files)
        stats["failed_files"] = failed_files
        if on_progress:
            _emit_progress("done", {"stats": stats})
        return stats

    try:
        rj_digits = extract_rj_number(rj_input)
        stats["rj"] = f"RJ{rj_digits}"
        log(f"[1/4] 识别 RJ 号: RJ{rj_digits}")
        output_base = output_dir if output_dir else get_default_output_dir()

        # 优先复用本地 manifest：有效则跳过 workInfo/tracks API，直接续传
        manifest_data, manifest_out_dir = _load_manifest(output_base, rj_digits)
        if manifest_data is not None:
            pending, out_dir = _use_manifest(manifest_data, manifest_out_dir, via_api_fallback=False)
            if pending is None:
                return stats
            selected = pending
            log(f"[4/4] 开始续传（{workers} 线程，重试 {retries} 次）...")
            return _run_downloads(selected)

        try:
            work_info, rj_used = fetch_work_info(rj_digits)
        except Exception as e:  # noqa: BLE001
            # workInfo API 失败：尝试用本地 manifest 兜底续传
            manifest_data, manifest_out_dir = _load_manifest(output_base, rj_digits)
            if manifest_data is None:
                raise
            log(f"[1/4] workInfo API 请求失败: {e}")
            pending, out_dir = _use_manifest(manifest_data, manifest_out_dir, via_api_fallback=True)
            if pending is None:
                return stats
            selected = pending
            log(f"[4/4] 开始续传（{workers} 线程，重试 {retries} 次）...")
            return _run_downloads(selected)

        title = extract_title(work_info)
        stats["title"] = title
        _emit_progress("meta", {"rj": stats["rj"], "title": title})

        folder_name = sanitize_filename(f"RJ{rj_digits}_{title}")
        out_dir = Path(output_base) / folder_name
        out_dir.mkdir(parents=True, exist_ok=True)
        stats["out_dir"] = str(out_dir)
        # 下载开始阶段：保存完整 meta 与封面，供本地作品库展示（失败不阻塞）
        _save_meta_and_cover(out_dir, work_info)
        log(f"[2/4] 作品: {title}")
        log(f"      输出目录: {out_dir}")

        tracks = fetch_tracks(rj_used)
        _check_cancel()
        leaves = collect_leaves(tracks)
        log(f"[3/4] 文件树叶子节点: {len(leaves)}")
        _emit_progress("tree", {"leaves": len(leaves)})

        inc_re = re.compile(include_regex) if include_regex else None
        exc_re = re.compile(exclude_regex) if exclude_regex else None
        types_set = {t.strip().lower() for t in types.split(",") if t.strip()}
        exts_set = {e.strip().lower().lstrip(".") for e in exts.split(",") if e.strip()}
        explicit_exclude = {e.strip().lower().lstrip(".") for e in exclude_exts.split(",") if e.strip()}
        # 显式 --ext / --exclude-ext 覆盖默认音轨策略（保留显式优先）
        use_default_strategy = not exts_set and not explicit_exclude

        # 先按 --types 过滤（type 过滤对两种模式都生效）
        type_filtered = []
        skipped_filter = 0
        for rel_dir, node in leaves:
            if not filter_leaf(node, types_set, set(), set()):
                skipped_filter += 1
                continue
            type_filtered.append((rel_dir, node))

        if use_default_strategy:
            selected, strategy_skipped = apply_default_audio_strategy(type_filtered)
            skipped_filter += strategy_skipped
            log("      音轨策略: 按去扩展名文件名归并，m4a 优先（含 wav/flac 转 m4a），仅无 m4a 取 mp3，每曲只留最高优先级一个格式，视频硬排除，文本/图片保留")
        else:
            exclude_set = set(explicit_exclude)
            selected = []
            for rel_dir, node in type_filtered:
                if not filter_leaf(node, set(), exts_set, exclude_set):
                    skipped_filter += 1
                    continue
                selected.append((rel_dir, node))
            log("      显式过滤: --ext/--exclude-ext 覆盖默认音轨策略")

        # 正则过滤（后置，对两种模式都生效）
        final_selected = []
        for rel_dir, node in selected:
            leaf_title = node.get("title") or ""
            if inc_re and not inc_re.search(leaf_title):
                skipped_filter += 1
                continue
            if exc_re and exc_re.search(leaf_title):
                skipped_filter += 1
                continue
            final_selected.append((rel_dir, node))
        selected = final_selected
        log(f"      命中过滤规则: {len(selected)}（过滤掉 {skipped_filter}）")
        if on_progress:
            _emit_progress("filter", {"selected": len(selected), "skipped": skipped_filter})
        if not selected:
            log("没有符合条件的文件，结束。")
            if on_progress:
                _emit_progress("done", {"stats": stats})
            return stats

        _save_manifest(out_dir, stats["rj"], title, selected)
        return _run_downloads(selected)
    except DownloadCancelled:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        stats["fail"] = stats["total"] if stats["total"] else 1
        stats["error"] = f"{type(e).__name__}: {e}"
        log(f"[失败] 作品处理异常: {stats['error']}")
        if on_progress:
            _emit_progress("done", {"stats": stats})
        return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="asmr_downloader",
        description="ASMR 作品下载器：根据 RJ 号获取元数据与文件树并并发下载。",
        epilog="默认音轨策略（格式全局屏蔽）：作品只要有 m4a（含 wav/flac 转 m4a）就全局剔除所有 mp3，"
               "全部音频走 m4a（原声 m4a 直下、wav/flac 由服务器转码为 m4a）；仅当作品完全没有 m4a/wav/flac "
               "时才下载 mp3；不做文件名/音轨匹配。mp4/mkv/webm/avi/mov 等视频格式及 type=video 节点一律不下载；"
               "文本/图片等非音频文件正常下载。显式提供 --ext 或 --exclude-ext 时覆盖默认策略，按指定扩展名过滤。",
    )
    parser.add_argument("input", nargs="+", help="ASMR 作品地址（如 https://asmr.one/work/RJ01310099）或 RJ 号（RJ01310099），可传多个")
    parser.add_argument("--input-file", default="", help="作品清单文件（每行一个 RJ/URL），与命令行 input 合并")
    parser.add_argument("-o", "--output", default=None, help="下载输出目录（不指定则自动使用系统下载目录）")
    parser.add_argument("--config", default="", help="配置文件 JSON 路径（默认读取脚本目录 config.json，若存在）")
    parser.add_argument("--types", default="", help="按 type 字段过滤，逗号分隔：text/audio/image/other")
    parser.add_argument("--ext", default="", help="按扩展名过滤，逗号分隔，如 mp3,wav；显式指定后覆盖默认音轨策略")
    parser.add_argument("--exclude-ext", default="", help="排除扩展名，逗号分隔，如 mp4,zip；显式指定后覆盖默认音轨策略")
    parser.add_argument("--include-regex", default="", help="按文件名（title）正则白名单过滤，后置过滤")
    parser.add_argument("--exclude-regex", default="", help="按文件名（title）正则黑名单过滤，后置过滤")
    parser.add_argument("-w", "--workers", type=positive_int, default=DEFAULT_WORKERS, help=f"并发线程数（默认 {DEFAULT_WORKERS}）")
    parser.add_argument("-r", "--retries", type=positive_int, default=DEFAULT_RETRIES, help=f"单文件失败重试次数（默认 {DEFAULT_RETRIES}）")
    parser.add_argument("--proxy", default="", help="代理地址 http://host:port（仅支持 http/https；SOCKS 代理不受支持）")
    parser.add_argument("--min-speed", type=positive_float, default=DEFAULT_MIN_SPEED, help=f"最低平均速度看门狗阈值 KB/s（默认 {DEFAULT_MIN_SPEED}）")
    parser.add_argument("--stall-time", type=positive_float, default=DEFAULT_STALL_TIME, help=f"平均速率低于阈值持续秒数后中断连接触发重试（默认 {DEFAULT_STALL_TIME}）")
    parser.add_argument("--log-file", default="", help="日志落盘文件路径（UTF-8，追加）")
    parser.add_argument("--no-preallocate", action="store_true", help="不预分配磁盘空间（稀疏式边下边写），磁盘占用按实际写入增长")
    parser.add_argument("--json-progress", action="store_true",
                        help="输出机器可读进度 JSON 行（##JSON## 前缀）到 stdout，供外部进程解析")
    args = parser.parse_args()

    def _json_progress(rj: str, stage: str, data: dict) -> None:
        """机器可读进度输出：##JSON## 前缀 + 单行 JSON，UTF-8 写 stdout（不受控制台编码影响）。"""
        line = "##JSON##" + json.dumps(
            {"event": "progress", "rj": rj, "stage": stage, "data": data},
            ensure_ascii=False, default=str,
        )
        sys.stdout.buffer.write(line.encode("utf-8", errors="replace") + b"\n")
        sys.stdout.buffer.flush()

    # P1-10 配置合并：环境变量 > CLI 显式参数 > config.json（默认脚本目录 config.json / --config 指定）
    if args.config:
        cfg_path = Path(args.config).expanduser()
    else:
        cfg_path = Path(__file__).resolve().parent / "config.json"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log(f"[配置] 警告: 读取配置文件 {cfg_path} 失败: {e}")
    if not args.proxy:
        args.proxy = os.environ.get("ASMR_PROXY", cfg.get("proxy", ""))
    if not args.output:
        args.output = os.environ.get("ASMR_OUTPUT", cfg.get("output", None))
    for key in ("types", "ext", "exclude_ext", "include_regex", "exclude_regex",
                "log_file", "input_file"):
        if not getattr(args, key):
            setattr(args, key, cfg.get(key, ""))

    if args.log_file:
        set_log_file(args.log_file)
        log(f"[日志] 落盘: {args.log_file}")

    setup_network(args.proxy)

    if not args.output:
        log(f"      未指定 -o，默认下载到当前目录下的 download 子目录: {get_default_output_dir()}")
    log(f"      代理: {args.proxy or '系统默认'} | 并发 {args.workers} | 重试 {args.retries}")

    inputs = list(args.input)
    if args.input_file:
        try:
            with open(args.input_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        inputs.append(line)
        except OSError as e:
            log(f"[参数] 读取 --input-file 失败: {e}")
            return 2
    if not inputs:
        log("没有可下载的作品。")
        return 0

    # 批量汇总统计（P1-08）
    summary = {"works": 0, "total": 0, "success": 0, "skip": 0, "fail": 0,
               "failed_files": [], "started": time.monotonic()}
    exit_code = 0
    try:
        for idx, rj_input in enumerate(inputs, 1):
            log(f"\n===== 作品 [{idx}/{len(inputs)}]: {rj_input} =====")
            on_progress = None
            if args.json_progress:
                on_progress = lambda stage, data: _json_progress(rj_input, stage, data)  # noqa: E731
            stats = download_work(
                rj_input, output_dir=args.output, types=args.types, exts=args.ext,
                exclude_exts=args.exclude_ext, include_regex=args.include_regex,
                exclude_regex=args.exclude_regex, workers=args.workers, retries=args.retries,
                min_speed=args.min_speed, stall_time=args.stall_time,
                preallocate=not args.no_preallocate,
                on_progress=on_progress,
            )
            summary["works"] += 1
            summary["total"] += stats["total"]
            summary["success"] += stats["success"]
            summary["skip"] += stats["skip"]
            summary["fail"] += stats["fail"]
            summary["failed_files"].extend(stats.get("failed_files", []))
            if stats.get("error"):
                summary["failed_files"].append(f"{stats['rj']}: {stats['error']}")
                exit_code = 1
            elif stats["fail"]:
                exit_code = 1
            if idx < len(inputs):
                # 多作品之间错开启动间隔（历史经验：同启触发代理 TLS 重置）
                time.sleep(2.0)
    except KeyboardInterrupt:
        log("\n已中断。未完成文件保留 .part 与 .part.meta.json，下次运行自动续传。")
        return 130

    elapsed = time.monotonic() - summary["started"]
    log("\n===== 批量下载汇总 =====")
    log(f"  作品: {summary['works']} | 文件: 总 {summary['total']} / 成功 {summary['success']} / 跳过 {summary['skip']} / 失败 {summary['fail']}")
    log(f"  总耗时: {elapsed:.1f}s")
    if summary["fail"]:
        log(f"  失败文件 ({len(summary['failed_files'])}):")
        for f in summary["failed_files"]:
            if isinstance(f, dict):
                log(f"    - {f.get('path')}（{f.get('reason')}）")
            else:
                log(f"    - {f}")
    else:
        log("  全部成功。")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
