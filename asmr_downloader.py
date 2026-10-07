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
DEFAULT_RETRIES = 3
CHUNK_SIZE = 256 * 1024
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) asmr-downloader/1.0"

_print_lock = threading.Lock()
_state_lock = threading.Lock()

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


def http_get_json(url: str, timeout: int = 30) -> object:
    """GET 请求并解析 JSON。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def get_download_dir() -> str:
    """解析系统标准下载目录的真实路径。

    Windows 通过 SHGetKnownFolderPath（FOLDERID_Downloads）读取真实下载目录，
    避免硬编码；其他平台或解析失败时回退到 ~/Downloads。
    """
    if os.name == "nt":
        try:
            import ctypes
            import uuid
            from ctypes import wintypes

            class _GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_byte * 8),
                ]

            folder = _GUID()
            u = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")  # FOLDERID_Downloads
            folder.Data1 = u.time_low
            folder.Data2 = u.time_mid
            folder.Data3 = u.time_hi_version
            folder.Data4 = (ctypes.c_byte * 8).from_buffer_copy(u.bytes[8:])

            fn = ctypes.windll.shell32.SHGetKnownFolderPath
            fn.argtypes = [
                ctypes.POINTER(_GUID),
                wintypes.DWORD,
                wintypes.HANDLE,
                ctypes.POINTER(ctypes.c_wchar_p),
            ]
            fn.restype = ctypes.c_long
            path_ptr = ctypes.c_wchar_p()
            if fn(ctypes.byref(folder), 0, None, ctypes.byref(path_ptr)) == 0 and path_ptr.value:
                path = path_ptr.value
                ctypes.windll.ole32.CoTaskMemFree(path_ptr)
                return path
        except Exception:  # noqa: BLE001
            pass
    return str(Path.home() / "Downloads")


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


def download_file(url: str, dest: Path, expected_size: int, retries: int) -> bool:
    """下载单个文件：先写 .part，成功后原子改名；失败重试。"""
    part_path = dest.with_name(dest.name + ".part")
    for attempt in range(1, retries + 1):
        try:
            if part_path.exists():
                part_path.unlink()
            req = urllib.request.Request(
                quote_url(url), headers={"User-Agent": UA}
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(part_path, "wb") as fh:
                    while True:
                        chunk = resp.read(CHUNK_SIZE)
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
    args = parser.parse_args()

    types = {t.strip().lower() for t in args.types.split(",") if t.strip()}
    exts = {e.strip().lower().lstrip(".") for e in args.ext.split(",") if e.strip()}
    exclude_exts = {e.strip().lower().lstrip(".") for e in args.exclude_ext.split(",") if e.strip()}

    # 1. 提取 RJ 号
    rj_digits = extract_rj_number(args.input)
    log(f"[1/4] 识别 RJ 号: RJ{rj_digits}")

    # 2. 获取元数据
    work_info, rj_used = fetch_work_info(rj_digits)
    title = extract_title(work_info)
    folder_name = sanitize_filename(f"RJ{rj_digits}_{title}")
    output_base = args.output if args.output else get_download_dir()
    if not args.output:
        log(f"      未指定 -o，自动使用系统下载目录: {output_base}")
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
        expected = int(node.get("size") or 0)
        if dest.exists() and (not expected or dest.stat().st_size == expected):
            progress.tick("skip")
            return None
        ok = download_file(node["mediaDownloadUrl"], dest, expected, args.retries)
        progress.tick("ok" if ok else "fail")
        if not ok:
            return str(dest)
        return None

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(worker, item) for item in selected]
        for fut in as_completed(futures):
            err = fut.result()
            if err:
                failed_files.append(err)

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
