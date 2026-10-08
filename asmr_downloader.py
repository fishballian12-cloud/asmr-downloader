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
import json
import os
import re
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
DEFAULT_EXCLUDE_EXTS = {"wav", "flac"}
CHUNK_SIZE = 256 * 1024
MAX_ACTIVE_CHUNKS = 16
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) asmr-downloader/1.0"

_print_lock = threading.Lock()
_state_lock = threading.Lock()
_chunk_semaphore = threading.BoundedSemaphore(MAX_ACTIVE_CHUNKS)

# 全局 opener（--proxy 配置后生效）；None 表示使用系统默认（系统代理/直连）
_global_opener = None
# Range 支持探测缓存（按 host）
_range_support_cache = {}

# Windows 文件名非法字符（含控制字符）
INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Windows 保留设备名
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def log(msg: str) -> None:
    """线程安全打印。"""
    with _print_lock:
        print(msg, flush=True)


def setup_network(proxy: str = "") -> None:
    """根据 --proxy 配置全局 opener；未指定代理时保持系统代理/直连默认行为。

    仅支持 http:// 与 https:// 代理；SOCKS 代理（socks5:// 等）不受支持，忽略并警告。
    """
    global _global_opener
    if not proxy:
        _global_opener = None
        return
    m = re.match(r"^(?P<scheme>[a-z][a-z0-9+.-]*)://", proxy.strip())
    scheme = m.group("scheme").lower() if m else ""
    if scheme in ("socks", "socks4", "socks5"):
        log("[代理] 警告: 不支持 SOCKS 代理，忽略 --proxy，使用系统默认网络")
        _global_opener = None
        return
    if scheme not in ("http", "https"):
        raise ValueError(f"不支持的代理协议: {proxy}（仅支持 http:// 或 https://）")
    _global_opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )
    log(f"[代理] 已启用代理: {proxy}")


def _open_url(req, timeout: int):
    """打开请求：已配置代理时走全局 opener，否则用默认 urlopen（系统代理/直连）。"""
    if _global_opener is not None:
        return _global_opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def http_get_json(url: str, timeout: int = 30) -> object:
    """GET 请求并解析 JSON。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with _open_url(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


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
    """探测服务器是否支持 HTTP Range：用 Range: bytes=0-0 试探，206 即支持。按 host 缓存。"""
    host = urllib.parse.urlparse(url).netloc
    if host in _range_support_cache:
        return _range_support_cache[host]
    supported = False
    try:
        req = urllib.request.Request(
            quote_url(url), headers={"User-Agent": UA, "Range": "bytes=0-0"}
        )
        with _open_url(req, timeout=CONNECT_TIMEOUT) as resp:
            if resp.getcode() == 206:
                supported = True
            elif resp.headers.get("Accept-Ranges", "").lower() == "bytes":
                supported = True
    except Exception:  # noqa: BLE001
        supported = False
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


def download_file(url: str, dest: Path, expected_size: int, retries: int) -> bool:
    """下载单个文件：支持 Range 时走分片断点续传，否则退化整文件流式下载。"""
    part_path = dest.with_name(dest.name + ".part")
    meta_path = dest.with_name(dest.name + ".part.meta.json")
    read_timeout = read_timeout_for_size(expected_size)
    if expected_size > 0 and probe_range_support(url):
        log(f"    [Range] {dest.name}: 分片下载（{len(plan_chunks(expected_size))} 片）")
        return download_file_range(url, part_path, meta_path, dest, expected_size, retries, read_timeout)
    if meta_path.exists():
        # 服务器不支持 Range：清理可能残留的分片 meta，走整文件下载
        meta_path.unlink()
    log(f"    [整文件] {dest.name}: 服务器不支持 Range，流式下载")
    return download_file_whole(url, part_path, dest, expected_size, retries, read_timeout)


def download_file_whole(url: str, part_path: Path, dest: Path, expected_size: int, retries: int, read_timeout: int) -> bool:
    """整文件流式下载（Range 退化路径）：先写 .part，成功后原子改名；连接/读超时独立兜底重试。"""
    for attempt in range(1, retries + 1):
        try:
            if part_path.exists():
                part_path.unlink()
            req = urllib.request.Request(
                quote_url(url), headers={"User-Agent": UA}
            )
            # 连接阶段独立短超时；连接成功后按文件大小调整读超时
            with _open_url(req, timeout=CONNECT_TIMEOUT) as resp:
                _set_read_timeout(resp, read_timeout)
                with open(part_path, "wb") as fh:
                    while True:
                        try:
                            chunk = resp.read(CHUNK_SIZE)
                        except (TimeoutError, socket.timeout) as e:
                            # 连续无数据超时：本轮视为失败，交由外层重试
                            raise IOError(
                                f"读取数据超时（{read_timeout}s 内无数据）: {dest.name}"
                            ) from e
                        if not chunk:
                            break
                        fh.write(chunk)
            if expected_size and part_path.stat().st_size != expected_size:
                raise IOError(
                    f"大小不符: 期望 {expected_size} B, 实际 {part_path.stat().st_size} B"
                )
            part_path.replace(dest)
            return True
        except Exception as e:  # noqa: BLE001
            log(f"    [重试] {dest.name} 第 {attempt}/{retries} 次失败: {e}")
            if attempt < retries:
                time.sleep(1.5 * attempt)
    return False


def download_chunk(url: str, part_path: Path, start: int, end: int, read_timeout: int, retries: int) -> bool:
    """用 Range 下载一个分片到 .part 对应偏移；分片内部重试，耗尽后返回 False。

    受全局信号量约束，控制总活跃分片数，避免与文件级并发叠加导致连接爆炸。
    """
    with _chunk_semaphore:
        for attempt in range(1, retries + 1):
            try:
                req = urllib.request.Request(
                    quote_url(url),
                    headers={"User-Agent": UA, "Range": f"bytes={start}-{end}"},
                )
                with _open_url(req, timeout=CONNECT_TIMEOUT) as resp:
                    if resp.getcode() != 206:
                        raise IOError(f"服务器忽略 Range（HTTP {resp.getcode()}）")
                    _set_read_timeout(resp, read_timeout)
                    rstart = start
                    cr = resp.headers.get("Content-Range", "")
                    m = re.match(r"bytes\s+(\d+)-(\d+)/", cr)
                    if m:
                        rstart = int(m.group(1))
                    if rstart != start:
                        raise IOError(f"Range 响应偏移不符: 请求 {start}, 响应 {rstart}")
                    with open(part_path, "r+b") as fh:
                        fh.seek(rstart)
                        remaining = end - rstart + 1
                        while remaining > 0:
                            try:
                                chunk = resp.read(min(CHUNK_SIZE, remaining))
                            except (TimeoutError, socket.timeout) as e:
                                raise IOError(
                                    f"读取分片超时（{read_timeout}s 内无数据）: {part_path.name} [{start}-{end}]"
                                ) from e
                            if not chunk:
                                raise IOError(
                                    f"分片提前结束（连接中断）: {part_path.name} [{start}-{end}]"
                                )
                            fh.write(chunk)
                            remaining -= len(chunk)
                return True
            except Exception as e:  # noqa: BLE001
                log(f"      [分片重试] {part_path.name} [{start}-{end}] 第 {attempt}/{retries} 次失败: {e}")
                if attempt < retries:
                    time.sleep(1.5 * attempt)
    return False


def download_file_range(url: str, part_path: Path, meta_path: Path, dest: Path, total_size: int, retries: int, read_timeout: int) -> bool:
    """Range 分片下载 + 分片级断点续传。

    - .part 按偏移写入各分片；完成状态持久化到 .part.meta.json。
    - 重试时仅重下未完成分片，已完成分片跳过。
    - 全部片完成后校验总大小，原子改名去掉 .part，删除 meta。
    - 任一单片重试耗尽则文件标记失败，但保留已完成的片与 meta 供下次续传。
    """
    chunks = plan_chunks(total_size)
    meta = load_chunk_meta(meta_path)
    if meta is None or meta.get("total_size") != total_size or len(meta.get("chunks", [])) != len(chunks):
        # 全新（或规划变化）状态：重置 part 为完整大小，保证各分片可随机写
        chunk_states = [
            {"start": s, "end": e, "completed": False}
            for (s, e) in chunks
        ]
        with open(part_path, "wb") as fh:
            fh.truncate(total_size)
        meta = {"total_size": total_size, "chunks": chunk_states}
        save_chunk_meta(meta_path, meta)
    chunk_states = meta["chunks"]

    failed_any = False
    pending = [c for c in chunk_states if not c["completed"]]
    while pending and not failed_any:
        with ThreadPoolExecutor(max_workers=min(4, len(pending))) as pool:
            futures = {
                pool.submit(download_chunk, url, part_path, c["start"], c["end"], read_timeout, retries): c
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


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="asmr_downloader",
        description="ASMR 作品下载器：根据 RJ 号获取元数据与文件树并并发下载。",
    )
    parser.add_argument("input", help="ASMR 作品地址（如 https://asmr.one/work/RJ01310099）或 RJ 号（RJ01310099）")
    parser.add_argument("-o", "--output", default=None, help="下载输出目录（不指定则自动使用系统下载目录）")
    parser.add_argument("--types", default="", help="按 type 字段过滤，逗号分隔：text/audio/image/other")
    parser.add_argument("--ext", default="", help="按扩展名过滤，逗号分隔，如 mp3,wav")
    parser.add_argument("--exclude-ext", default="", help="排除扩展名，逗号分隔，如 mp4,zip")
    parser.add_argument("-w", "--workers", type=int, default=DEFAULT_WORKERS, help=f"并发线程数（默认 {DEFAULT_WORKERS}）")
    parser.add_argument("-r", "--retries", type=int, default=DEFAULT_RETRIES, help=f"单文件失败重试次数（默认 {DEFAULT_RETRIES}）")
    parser.add_argument("--proxy", default="", help="代理地址 http://host:port（仅支持 http/https；SOCKS 代理不受支持）")
    args = parser.parse_args()

    setup_network(args.proxy)

    types = {t.strip().lower() for t in args.types.split(",") if t.strip()}
    exts = {e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()}
    explicit_exclude = {e.strip().lower().lstrip(".") for e in args.exclude_ext.split(",") if e.strip()}

    # 默认过滤规则：默认排除 wav/flac 节省流量；--ext 白名单时默认排除不生效
    if exts:
        exclude_exts = set(explicit_exclude)
        if explicit_exclude:
            log(f"      过滤规则: --ext 白名单 [{','.join(sorted(exts))}]；显式排除 [{','.join(sorted(explicit_exclude))}]（默认排除 wav/flac 不生效）")
        else:
            log(f"      过滤规则: --ext 白名单 [{','.join(sorted(exts))}]（默认排除 wav/flac 不生效）")
    else:
        exclude_exts = set(DEFAULT_EXCLUDE_EXTS) | explicit_exclude
        if explicit_exclude:
            log(f"      过滤规则: 默认排除 [{','.join(sorted(DEFAULT_EXCLUDE_EXTS))}] ∪ 显式排除 [{','.join(sorted(explicit_exclude))}] = [{','.join(sorted(exclude_exts))}]")
        else:
            log(f"      过滤规则: 默认排除 [{','.join(sorted(exclude_exts))}]")

    # 1. 提取 RJ 号
    rj_digits = extract_rj_number(args.input)
    log(f"[1/4] 识别 RJ 号: RJ{rj_digits}")

    # 2. 获取元数据
    work_info, rj_used = fetch_work_info(rj_digits)
    title = extract_title(work_info)
    folder_name = sanitize_filename(f"RJ{rj_digits}_{title}")
    output_base = args.output if args.output else get_default_output_dir()
    if not args.output:
        log(f"      未指定 -o，默认下载到当前目录下的 download 子目录: {output_base}")
        Path(output_base).mkdir(parents=True, exist_ok=True)
    out_dir = Path(output_base) / folder_name
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"[2/4] 作品: {title}")
    log(f"      输出目录: {out_dir}")

    # 3. 获取文件树并过滤
    tracks = fetch_tracks(rj_used)
    leaves = collect_leaves(tracks)
    log(f"[3/4] 文件树叶子节点: {len(leaves)}")

    selected = []
    skipped_filter = 0
    for rel_dir, node in leaves:
        if filter_leaf(node, types, exts, exclude_exts):
            selected.append((rel_dir, node))
        else:
            skipped_filter += 1
    log(f"      命中过滤规则: {len(selected)}（过滤掉 {skipped_filter}）")
    if not selected:
        log("没有符合条件的文件，结束。")
        return 0

    # 4. 并发下载
    log(f"[4/4] 开始下载（{args.workers} 线程，重试 {args.retries} 次）...")
    progress = Progress(len(selected))
    failed_files = []

    def worker(item):
        rel_dir, node = item
        dest = out_dir / rel_dir / sanitize_filename(node.get("title") or "untitled")
        dest.parent.mkdir(parents=True, exist_ok=True)
        expected = int(node.get("size") or 0)
        if dest.exists() and (not expected or dest.stat().st_size == expected):
            progress.tick("skip")
            return None
        ok = download_file(node["mediaDownloadUrl"], dest, expected, args.retries)
        progress.tick("ok" if ok else "fail")
        if not ok:
            return str(dest)
        return None

    pool = ThreadPoolExecutor(max_workers=args.workers)
    try:
        futures = [pool.submit(worker, item) for item in selected]
        for fut in as_completed(futures):
            try:
                # 结果超时保护：正常已完成的 future 应立即返回
                err = fut.result(timeout=30)
            except Exception as e:  # noqa: BLE001
                err = f"下载线程异常: {e}"
            if err:
                failed_files.append(err)
    finally:
        # 兜底收尾：取消未启动任务，等待已启动任务（worker 内网络均有超时，不会永久阻塞）
        pool.shutdown(wait=True, cancel_futures=True)

    log("下载完成。")
    if progress.skipped:
        log(f"  跳过已存在完整文件: {progress.skipped}")
    if failed_files:
        log(f"  失败文件 ({len(failed_files)}):")
        for f in failed_files:
            log(f"    - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
