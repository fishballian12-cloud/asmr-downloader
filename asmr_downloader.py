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
import http.client
import json
import os
import random
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

API_BASE = "https://api.asmr-200.com/api"
DEFAULT_WORKERS = 8
DEFAULT_RETRIES = 5
CONNECT_TIMEOUT = 30
DEFAULT_EXCLUDE_EXTS = {"wav", "flac", "mp4"}
CHUNK_SIZE = 256 * 1024
MAX_ACTIVE_CHUNKS = 16
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) asmr-downloader/1.0"
DEFAULT_MIN_SPEED = 20.0          # 最低平均速度看门狗阈值（KB/s）
DEFAULT_STALL_TIME = 30.0         # 平均速率低于阈值持续秒数后中断连接触发重试
WATCHDOG_CHECK_BYTES = 1024 * 1024  # 每写入约 1MB 检查一次平均速率
API_RETRY_TIMES = 3               # API 请求（元数据/文件树）网络类失败重试次数
API_RETRY_BASE_DELAY = 1.0        # API 重试指数退避基数（秒）
MAX_JSON_BODY = 50 * 1024 * 1024  # API JSON 响应体上限（50MB），防内存暴涨
DISK_SPACE_FACTOR = 1.1           # 磁盘预检冗余系数：预留 1.1 倍文件大小空间

_print_lock = threading.Lock()
_state_lock = threading.Lock()
_chunk_semaphore = threading.BoundedSemaphore(MAX_ACTIVE_CHUNKS)

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
    http.client.HTTPException,  # 连接池/协议层异常（BadStatusLine 等）
)


def _is_retryable(exc: BaseException) -> bool:
    """判断异常是否属于可重试的网络类错误（含看门狗/服务端瞬时错误）。"""
    return isinstance(exc, RETRYABLE_EXCEPTIONS) or isinstance(exc, (StallError, RetryableHTTPError))


def _sleep_backoff(attempt: int) -> None:
    """指数退避 + 随机抖动，避免多线程同步重试风暴。"""
    base = API_RETRY_BASE_DELAY * (2 ** max(0, attempt - 1))
    time.sleep(max(0.0, base + random.uniform(0, 0.5)))


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


class ConnectionPool:
    """per-(scheme, host, port, proxy) 连接池，线程安全：获取/归还/失效重建。"""

    def __init__(self, scheme: str, host: str, port: int, proxy: str, timeout: int):
        self.scheme = scheme
        self.host = host
        self.port = port
        self.proxy = proxy
        self.timeout = timeout
        self._idle = []
        self._lock = threading.Lock()

    def acquire(self) -> _PooledConnection:
        with self._lock:
            while self._idle:
                pc = self._idle.pop()
                if pc.alive():
                    pc.in_use = True
                    return pc
                pc.close()
        return _PooledConnection(self, self.scheme, self.host, self.port, self.proxy, self.timeout)

    def release(self, pc: _PooledConnection, reusable: bool) -> None:
        if pc.dead or not reusable:
            pc.close()
            return
        pc.in_use = False
        with self._lock:
            self._idle.append(pc)


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


def plan_chunks(total_size: int) -> list:
    """按文件大小规划分片：<5MB 1 片；5~50MB 4 片；>50MB 8 片。返回 [(start, end), ...]。"""
    MB = 1024 * 1024
    if total_size < 5 * MB:
        n = 1
    elif total_size <= 50 * MB:
        n = 4
    else:
        n = 8
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
                  preallocate: bool = True) -> bool:
    """下载单个文件：支持 Range 时走分片断点续传，否则退化整文件流式下载。"""
    part_path = dest.with_name(dest.name + ".part")
    meta_path = dest.with_name(dest.name + ".part.meta.json")
    read_timeout = read_timeout_for_size(expected_size)
    _ensure_disk_space(dest, expected_size)
    if expected_size > 0 and probe_range_support(url):
        log(f"    [Range] {dest.name}: 分片下载（{len(plan_chunks(expected_size))} 片）")
        return download_file_range(url, part_path, meta_path, dest, expected_size, retries, read_timeout, min_speed_kbps, stall_time, preallocate)
    if meta_path.exists():
        # 服务器不支持 Range：清理可能残留的分片 meta，走整文件下载
        meta_path.unlink()
    log(f"    [整文件] {dest.name}: 服务器不支持 Range，流式下载")
    return download_file_whole(url, part_path, dest, expected_size, retries, read_timeout, min_speed_kbps, stall_time)


def download_file_whole(url: str, part_path: Path, dest: Path, expected_size: int, retries: int,
                        read_timeout: int,
                        min_speed_kbps: float = DEFAULT_MIN_SPEED,
                        stall_time: float = DEFAULT_STALL_TIME) -> bool:
    """整文件流式下载（Range 退化路径）：先写 .part，成功后原子改名；连接/读超时独立兜底重试。"""
    for attempt in range(1, retries + 1):
        try:
            if part_path.exists():
                part_path.unlink()
            pc, resp = fetch_http(quote_url(url), read_timeout=min(read_timeout, stall_time))
            reusable = False
            try:
                if resp.status != 200:
                    if resp.status in (408, 429) or resp.status >= 500:
                        raise RetryableHTTPError(f"服务端瞬时错误 HTTP {resp.status}")
                    raise IOError(f"HTTP {resp.status}")
                conn_start = time.monotonic()
                total_bytes = 0
                last_check_bytes = 0
                last_check_time = conn_start
                stall_started = None
                # 读块随 stall_time×min_speed 收缩，保证低速率下检查频率足够（滴流也能按时中断）
                read_block = min(CHUNK_SIZE, max(32 * 1024, int(stall_time * min_speed_kbps * 1024 / 4)))
                with open(part_path, "wb") as fh:
                    while True:
                        try:
                            chunk = resp.read(read_block)
                        except (TimeoutError, socket.timeout) as e:
                            # 连续无数据超过 stall-time：最低速度看门狗触发，主动中断重试
                            raise StallError(
                                f"连续 {stall_time:.0f}s 无新数据（最低速度看门狗）: {dest.name}"
                            ) from e
                        if not chunk:
                            break
                        fh.write(chunk)
                        total_bytes += len(chunk)
                        now = time.monotonic()
                        if total_bytes - last_check_bytes >= WATCHDOG_CHECK_BYTES or now - last_check_time >= stall_time:
                            stall_started = _enforce_min_speed(
                                total_bytes, conn_start, stall_started,
                                min_speed_kbps * 1024, stall_time, now
                            )
                            last_check_bytes = total_bytes
                            last_check_time = now
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
            return True
        except StallError as e:
            log(f"    [重试] {dest.name} 第 {attempt}/{retries} 次失败(看门狗): {e}")
            if attempt < retries:
                _sleep_backoff(attempt)
        except RetryableHTTPError as e:
            log(f"    [重试] {dest.name} 第 {attempt}/{retries} 次失败(HTTP {e}): {e}")
            if attempt < retries:
                _sleep_backoff(attempt)
        except RETRYABLE_EXCEPTIONS as e:
            log(f"    [重试] {dest.name} 第 {attempt}/{retries} 次失败: {e}")
            if attempt < retries:
                _sleep_backoff(attempt)
        except Exception as e:
            log(f"    [失败] {dest.name} 不可重试错误({type(e).__name__}): {e}")
            return False
    return False


def download_chunk(url: str, part_path: Path, start: int, end: int, read_timeout: int, retries: int,
                   min_speed_kbps: float = DEFAULT_MIN_SPEED,
                   stall_time: float = DEFAULT_STALL_TIME) -> bool:
    """用 Range 下载一个分片到 .part 对应偏移；分片内部重试，耗尽后返回 False。

    受全局信号量约束，控制总活跃分片数，避免与文件级并发叠加导致连接爆炸。
    """
    with _chunk_semaphore:
        for attempt in range(1, retries + 1):
            try:
                pc, resp = fetch_http(quote_url(url), headers={"Range": f"bytes={start}-{end}"},
                                      read_timeout=min(read_timeout, stall_time))
                reusable = False
                try:
                    if resp.status != 206:
                        if resp.status in (408, 429) or resp.status >= 500:
                            raise RetryableHTTPError(f"服务端瞬时错误 HTTP {resp.status}")
                        raise IOError(f"服务器忽略 Range（HTTP {resp.status}）")
                    rstart = start
                    cr = resp.headers.get("Content-Range", "")
                    m = re.match(r"bytes\s+(\d+)-(\d+)/", cr)
                    if m:
                        rstart = int(m.group(1))
                    if rstart != start:
                        raise IOError(f"Range 响应偏移不符: 请求 {start}, 响应 {rstart}")
                    conn_start = time.monotonic()
                    total_bytes = 0
                    last_check_bytes = 0
                    last_check_time = conn_start
                    stall_started = None
                    # 读块随 stall_time×min_speed 收缩，保证低速率下检查频率足够（滴流也能按时中断）
                    read_block = min(CHUNK_SIZE, max(32 * 1024, int(stall_time * min_speed_kbps * 1024 / 4)))
                    with open(part_path, "r+b") as fh:
                        fh.seek(rstart)
                        remaining = end - rstart + 1
                        while remaining > 0:
                            try:
                                chunk = resp.read(min(read_block, remaining))
                            except (TimeoutError, socket.timeout) as e:
                                # 连续无数据超过 stall-time：最低速度看门狗触发，主动中断重试
                                raise StallError(
                                    f"连续 {stall_time:.0f}s 无新数据（最低速度看门狗）: {part_path.name} [{start}-{end}]"
                                ) from e
                            if not chunk:
                                raise IOError(
                                    f"分片提前结束（连接中断）: {part_path.name} [{start}-{end}]"
                                )
                            fh.write(chunk)
                            total_bytes += len(chunk)
                            remaining -= len(chunk)
                            now = time.monotonic()
                            if total_bytes - last_check_bytes >= WATCHDOG_CHECK_BYTES or now - last_check_time >= stall_time:
                                stall_started = _enforce_min_speed(
                                    total_bytes, conn_start, stall_started,
                                    min_speed_kbps * 1024, stall_time, now
                                )
                                last_check_bytes = total_bytes
                                last_check_time = now
                    reusable = True  # body 已读尽，连接可复用
                    elapsed = time.monotonic() - conn_start
                    if elapsed > 0:
                        log(f"      [分片完成] {part_path.name} [{start}-{end}] 平均速度 {format_speed(total_bytes / elapsed)}")
                finally:
                    pc.release(reusable)
                return True
            except StallError as e:
                log(f"      [分片重试] {part_path.name} [{start}-{end}] 第 {attempt}/{retries} 次失败(看门狗): {e}")
                if attempt < retries:
                    _sleep_backoff(attempt)
            except RetryableHTTPError as e:
                log(f"      [分片重试] {part_path.name} [{start}-{end}] 第 {attempt}/{retries} 次失败(HTTP {e}): {e}")
                if attempt < retries:
                    _sleep_backoff(attempt)
            except RETRYABLE_EXCEPTIONS as e:
                log(f"      [分片重试] {part_path.name} [{start}-{end}] 第 {attempt}/{retries} 次失败: {e}")
                if attempt < retries:
                    _sleep_backoff(attempt)
            except Exception as e:
                log(f"      [分片失败] {part_path.name} [{start}-{end}] 不可重试错误({type(e).__name__}): {e}")
                return False
    return False


def download_file_range(url: str, part_path: Path, meta_path: Path, dest: Path, total_size: int, retries: int,
                        read_timeout: int,
                        min_speed_kbps: float = DEFAULT_MIN_SPEED,
                        stall_time: float = DEFAULT_STALL_TIME,
                        preallocate: bool = True) -> bool:
    """Range 分片下载 + 分片级断点续传。

    - .part 按偏移写入各分片；完成状态持久化到 .part.meta.json。
    - 入口自愈：meta 无效、或 .part 缺失 / 大小与预期不符（预分配模式）时，统一重置分片状态。
    - 重试时仅重下未完成分片，已完成分片跳过。
    - 全部片完成后校验总大小，原子改名去掉 .part，删除 meta。
    - 任一单片重试耗尽则文件标记失败，但保留已完成的片与 meta 供下次续传。
    - preallocate=False 时不做全量预分配，分片按偏移写入时自动扩展（稀疏式边下边写）。
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

    failed_any = False
    pending = [c for c in chunk_states if not c["completed"]]
    # 线程池在文件级复用（P2-18）：跨轮重试不再反复创建/销毁线程
    pool = ThreadPoolExecutor(max_workers=min(4, max(1, len(chunks))))
    try:
        while pending and not failed_any:
            futures = {
                pool.submit(download_chunk, url, part_path, c["start"], c["end"], read_timeout, retries, min_speed_kbps, stall_time): c
                for c in pending
            }
            for fut in as_completed(futures):
                c = futures[fut]
                try:
                    ok = fut.result(timeout=30)
                except Exception as e:  # noqa: BLE001
                    log(f"      [分片线程异常] {part_path.name} [{c['start']}-{c['end']}]: {e}")
                    ok = False
                if ok:
                    c["completed"] = True
                else:
                    failed_any = True
            save_chunk_meta(meta_path, meta)
            pending = [c for c in chunk_states if not c["completed"]]
    finally:
        pool.shutdown(wait=True)

    if failed_any:
        log(f"    [分片失败] {part_path.name}: 存在重试耗尽的分片，保留 .part 与 .meta 供下次续传")
        return False
    if part_path.stat().st_size != total_size:
        log(f"    [分片失败] {part_path.name}: 大小校验失败 {part_path.stat().st_size} != {total_size}")
        return False
    part_path.replace(dest)
    meta_path.unlink()
    return True


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


def download_work(rj_input: str, output_dir: str = "", types: str = "", exts: str = "",
                  exclude_exts: str = "", include_regex: str = "", exclude_regex: str = "",
                  workers: int = DEFAULT_WORKERS, retries: int = DEFAULT_RETRIES,
                  min_speed: float = DEFAULT_MIN_SPEED, stall_time: float = DEFAULT_STALL_TIME,
                  preallocate: bool = True, on_progress=None) -> dict:
    """下载单个作品完整流程：元数据 → 文件树 → 过滤 → 并发下载。

    CLI 与 server 共用入口（模块化重构核心）。保持 CLI 语义向后兼容。
    on_progress(stage, data) 可选进度回调：
      - stage "meta":   {"rj", "title"}
      - stage "tree":   {"leaves"}
      - stage "filter": {"selected", "skipped"}
      - stage "file":   {"index", "total", "path", "ok", "skip"(可选)}
      - stage "done":   {"stats"}
    返回 stats dict：{rj, title, out_dir, total, success, skip, fail, failed_files,
                      error(可选)}
    """
    stats = {"rj": "", "title": "", "out_dir": "", "total": 0, "success": 0,
             "skip": 0, "fail": 0, "failed_files": []}
    try:
        rj_digits = extract_rj_number(rj_input)
        stats["rj"] = f"RJ{rj_digits}"
        log(f"[1/4] 识别 RJ 号: RJ{rj_digits}")

        work_info, rj_used = fetch_work_info(rj_digits)
        title = extract_title(work_info)
        stats["title"] = title
        if on_progress:
            on_progress("meta", {"rj": stats["rj"], "title": title})

        folder_name = sanitize_filename(f"RJ{rj_digits}_{title}")
        output_base = output_dir if output_dir else get_default_output_dir()
        out_dir = Path(output_base) / folder_name
        out_dir.mkdir(parents=True, exist_ok=True)
        stats["out_dir"] = str(out_dir)
        log(f"[2/4] 作品: {title}")
        log(f"      输出目录: {out_dir}")

        tracks = fetch_tracks(rj_used)
        leaves = collect_leaves(tracks)
        log(f"[3/4] 文件树叶子节点: {len(leaves)}")
        if on_progress:
            on_progress("tree", {"leaves": len(leaves)})

        inc_re = re.compile(include_regex) if include_regex else None
        exc_re = re.compile(exclude_regex) if exclude_regex else None
        types_set = {t.strip().lower() for t in types.split(",") if t.strip()}
        exts_set = {e.strip().lower().lstrip(".") for e in exts.split(",") if e.strip()}
        explicit_exclude = {e.strip().lower().lstrip(".") for e in exclude_exts.split(",") if e.strip()}
        if exts_set:
            exclude_set = set(explicit_exclude)
        else:
            exclude_set = set(DEFAULT_EXCLUDE_EXTS) | explicit_exclude

        selected = []
        skipped_filter = 0
        for rel_dir, node in leaves:
            if not filter_leaf(node, types_set, exts_set, exclude_set):
                skipped_filter += 1
                continue
            leaf_title = node.get("title") or ""
            if inc_re and not inc_re.search(leaf_title):
                skipped_filter += 1
                continue
            if exc_re and exc_re.search(leaf_title):
                skipped_filter += 1
                continue
            selected.append((rel_dir, node))
        log(f"      命中过滤规则: {len(selected)}（过滤掉 {skipped_filter}）")
        if on_progress:
            on_progress("filter", {"selected": len(selected), "skipped": skipped_filter})
        if not selected:
            log("没有符合条件的文件，结束。")
            if on_progress:
                on_progress("done", {"stats": stats})
            return stats

        stats["total"] = len(selected)
        log(f"[4/4] 开始下载（{workers} 线程，重试 {retries} 次）...")
        progress = Progress(len(selected))
        used_dest_names = set()  # P1-07: 同目录内文件名冲突唯一化

        def worker(item):
            rel_dir, node = item
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
            expected = int(node.get("size") or 0)
            if dest.exists() and (not expected or dest.stat().st_size == expected):
                progress.tick("skip")
                if on_progress:
                    on_progress("file", {"index": progress.done, "total": progress.total,
                                         "path": str(dest), "ok": True, "skip": True})
                return None
            ok = download_file(node["mediaDownloadUrl"], dest, expected, retries, min_speed,
                               stall_time, preallocate=preallocate)
            progress.tick("ok" if ok else "fail")
            if on_progress:
                on_progress("file", {"index": progress.done, "total": progress.total,
                                     "path": str(dest), "ok": ok})
            if not ok:
                return str(dest)
            return None

        pool = ThreadPoolExecutor(max_workers=workers)
        failed_files = []
        try:
            futures = [pool.submit(worker, item) for item in selected]
            for fut in as_completed(futures):
                try:
                    err = fut.result(timeout=30)
                except Exception as e:  # noqa: BLE001
                    err = f"下载线程异常: {e}"
                if err:
                    failed_files.append(err)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        stats["success"] = progress.ok
        stats["skip"] = progress.skipped
        stats["fail"] = len(failed_files)
        stats["failed_files"] = failed_files
        if on_progress:
            on_progress("done", {"stats": stats})
        return stats
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        stats["fail"] = stats["total"] if stats["total"] else 1
        stats["error"] = f"{type(e).__name__}: {e}"
        log(f"[失败] 作品处理异常: {stats['error']}")
        if on_progress:
            on_progress("done", {"stats": stats})
        return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="asmr_downloader",
        description="ASMR 作品下载器：根据 RJ 号获取元数据与文件树并并发下载。",
    )
    parser.add_argument("input", nargs="+", help="ASMR 作品地址（如 https://asmr.one/work/RJ01310099）或 RJ 号（RJ01310099），可传多个")
    parser.add_argument("--input-file", default="", help="作品清单文件（每行一个 RJ/URL），与命令行 input 合并")
    parser.add_argument("-o", "--output", default=None, help="下载输出目录（不指定则自动使用系统下载目录）")
    parser.add_argument("--config", default="", help="配置文件 JSON 路径（默认读取脚本目录 config.json，若存在）")
    parser.add_argument("--types", default="", help="按 type 字段过滤，逗号分隔：text/audio/image/other")
    parser.add_argument("--ext", default="", help="按扩展名过滤，逗号分隔，如 mp3,wav")
    parser.add_argument("--exclude-ext", default="", help="排除扩展名，逗号分隔，如 mp4,zip")
    parser.add_argument("--include-regex", default="", help="按文件名（title）正则白名单过滤，后置过滤")
    parser.add_argument("--exclude-regex", default="", help="按文件名（title）正则黑名单过滤，后置过滤")
    parser.add_argument("-w", "--workers", type=positive_int, default=DEFAULT_WORKERS, help=f"并发线程数（默认 {DEFAULT_WORKERS}）")
    parser.add_argument("-r", "--retries", type=positive_int, default=DEFAULT_RETRIES, help=f"单文件失败重试次数（默认 {DEFAULT_RETRIES}）")
    parser.add_argument("--proxy", default="", help="代理地址 http://host:port（仅支持 http/https；SOCKS 代理不受支持）")
    parser.add_argument("--min-speed", type=positive_float, default=DEFAULT_MIN_SPEED, help=f"最低平均速度看门狗阈值 KB/s（默认 {DEFAULT_MIN_SPEED}）")
    parser.add_argument("--stall-time", type=positive_float, default=DEFAULT_STALL_TIME, help=f"平均速率低于阈值持续秒数后中断连接触发重试（默认 {DEFAULT_STALL_TIME}）")
    parser.add_argument("--log-file", default="", help="日志落盘文件路径（UTF-8，追加）")
    parser.add_argument("--no-preallocate", action="store_true", help="不预分配磁盘空间（稀疏式边下边写），磁盘占用按实际写入增长")
    args = parser.parse_args()

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
            stats = download_work(
                rj_input, output_dir=args.output, types=args.types, exts=args.ext,
                exclude_exts=args.exclude_ext, include_regex=args.include_regex,
                exclude_regex=args.exclude_regex, workers=args.workers, retries=args.retries,
                min_speed=args.min_speed, stall_time=args.stall_time,
                preallocate=not args.no_preallocate,
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
            log(f"    - {f}")
    else:
        log("  全部成功。")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
