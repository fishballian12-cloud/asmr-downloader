#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ASMR Downloader Web 管理端（Python 标准库实现，零第三方依赖）。

- ThreadingHTTPServer 同时托管静态前端与 JSON API
- 复用 asmr_downloader.py 的下载引擎（download_work），进度通过回调采集（不解析日志）
- 任务状态持久化到 server/tasks.json，重启不丢
- 默认监听 127.0.0.1:8005

启动:  python server/server.py [--host 127.0.0.1] [--port 8005]
"""

import argparse
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

# 使 server 可被作为脚本或模块运行（python server/server.py / python -m server.server）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asmr_downloader as ad  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent
WEB_DIR = SERVER_DIR / "web"
TASKS_JSON = SERVER_DIR / "tasks.json"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8005
DEFAULT_WORKERS = 2  # 并发下载任务数（每个任务内部还有文件级并发）
POLL_INTERVAL = 2.0

# 本地作品库：默认下载根目录（与 TaskRunner 默认输出一致：cwd/download）
DOWNLOAD_ROOT = Path(os.getcwd()) / "download"
META_JSON = "meta.json"
COVER_JPG = "cover.jpg"

# 作品文件类型分类（按扩展名，供 /api/work/{rj} 文件列表分组）
AUDIO_EXTS = {"m4a", "mp3", "wav", "flac", "aac", "ogg", "opus"}
IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "avif"}
TEXT_EXTS = {"txt", "lrc", "md", "json", "log", "srt", "vtt"}

# 静态文件 Content-Type 映射（mimetypes 对 m4a/flac 等返回不可用类型，需补全）
STATIC_CONTENT_TYPES = {
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".lrc": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}

_RJ_RE = re.compile(r"RJ(\d+)", re.IGNORECASE)


class _Tee:
    """双写输出流：控制台 + 磁盘日志文件（UTF-8 追加写，线程安全，写入即刷盘）。

    用于替换 sys.stdout / sys.stderr，使 server 自身与下载引擎（asmr_downloader.log）
    的全部 print 输出同时落到 server.log，不再依赖启动命令的 stdout/stderr 重定向。
    """

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log_file = log_file
        self._lock = threading.Lock()

    def write(self, data):
        if not data:
            return 0
        try:
            with self._lock:
                self._stream.write(data)
                if self._log_file is not None:
                    self._log_file.write(data)
                    self._log_file.flush()
        except (OSError, ValueError):
            pass
        return len(data)

    def flush(self):
        try:
            self._stream.flush()
            if self._log_file is not None:
                self._log_file.flush()
        except (OSError, ValueError):
            pass

    def close_handle(self):
        """atexit 关闭：先 flush，再释放文件句柄，防止解释器退出时再次写已关闭文件。"""
        try:
            with self._lock:
                if self._log_file is not None:
                    self._log_file.flush()
                    self._log_file.close()
                    self._log_file = None
        except (OSError, ValueError):
            pass

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def isatty(self):
        return False

    @property
    def closed(self):
        return False

    def __getattr__(self, name):
        return getattr(self._stream, name)


def install_file_logging(log_path=None):
    """把 sys.stdout / sys.stderr 替换为双写流（控制台 + 日志文件），返回日志文件路径。

    - 日志文件默认 server/server.log，UTF-8 追加写；
    - 文件句柄由进程生命周期管理（atexit 关闭），常驻进程不泄漏；
    - server 各线程与引擎 ad.log 的 print 均经双写流落盘。
    """
    log_path = Path(log_path) if log_path else SERVER_DIR / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)

    import atexit
    def _close():
        try:
            sys.stdout.close_handle()
        except Exception:
            pass
        try:
            sys.stderr.close_handle()
        except Exception:
            pass
    atexit.register(_close)

    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)
    return log_path


def classify_file_ext(name: str) -> str:
    """按扩展名将文件归类为 audio / image / text / other。"""
    ext = Path(name).suffix.lstrip(".").lower()
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in TEXT_EXTS:
        return "text"
    return "other"


def extract_rj_from_meta(meta: dict) -> str:
    """从 meta.json 中提取 RJ 数字标识（兼容 id / workId / rj 等字段）。"""
    for key in ("id", "workId", "rj", "rjNumber", "work_id"):
        val = meta.get(key)
        if val is None:
            continue
        text = str(val)
        m = _RJ_RE.search(text)
        if m:
            return f"RJ{m.group(1)}"
        if text.isdigit():
            return f"RJ{int(text)}"
    return ""


def iter_work_dirs(root: Path):
    """扫描下载根目录，产出所有含 meta.json 的作品目录（递归深度 ≤ 3，找到即止不深入）。"""
    if not root.is_dir():
        return
    stack = [root]
    depth = {str(root): 0}
    while stack:
        d = stack.pop()
        try:
            children = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            if (child / META_JSON).is_file():
                yield child
                continue
            nd = depth.get(str(d), 0) + 1
            if nd < 3:
                depth[str(child)] = nd
                stack.append(child)


def load_work_meta(work_dir: Path) -> dict:
    """读取作品目录下 meta.json；失败返回空 dict。"""
    try:
        data = json.loads((work_dir / META_JSON).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def resolve_work_dir(rj: str) -> Path | None:
    """按 RJ 号定位作品目录：优先目录名 RJ\\d+ 匹配，其次 meta 内 id 匹配。"""
    rj_digits = ""
    m = _RJ_RE.search(rj or "")
    if m:
        rj_digits = m.group(1)
    elif rj and rj.isdigit():
        rj_digits = rj
    key = rj_digits.lstrip("0")
    for work_dir in iter_work_dirs(DOWNLOAD_ROOT):
        nm = _RJ_RE.search(work_dir.name)
        if nm and key and nm.group(1).lstrip("0") == key:
            return work_dir
        if not nm or not key:
            meta = load_work_meta(work_dir)
            if key and extract_rj_from_meta(meta).lstrip("RJ").lstrip("0") == key:
                return work_dir
    return None


def build_work_summary(work_dir: Path) -> dict:
    """构建 /api/library 单条作品摘要 {rj, title, cover_url, meta}。"""
    meta = load_work_meta(work_dir)
    rj = extract_rj_from_meta(meta)
    if not rj:
        m = _RJ_RE.search(work_dir.name)
        rj = f"RJ{m.group(1)}" if m else ""
    title = meta.get("title") or meta.get("workTitle") or meta.get("name") or work_dir.name
    cover_rel = (work_dir.name + "/" + COVER_JPG) if (work_dir / COVER_JPG).is_file() else ""
    cover_url = f"/files/{quote(cover_rel)}" if cover_rel else ""
    return {"rj": rj, "title": title, "cover_url": cover_url, "meta": meta}


def list_work_files(work_dir: Path) -> list:
    """递归列出作品目录内文件（排除 meta.json / cover.jpg），按类型分组。"""
    files = []
    for p in sorted(work_dir.rglob("*"), key=lambda x: x.name.lower()):
        if not p.is_file():
            continue
        if p.name in (META_JSON, COVER_JPG):
            continue
        rel = str(p.relative_to(work_dir))
        files.append({
            # 带作品目录名前缀的完整相对路径，前端 workFileUrl 直接拼 /files/ 即可访问
            "path": f"{work_dir.name}/{rel.replace(chr(92), '/')}",
            "name": p.name,
            "size": p.stat().st_size,
            "type": classify_file_ext(p.name),
        })
    return files

TASK_STATUS = ("queued", "running", "done", "failed", "cancelled")


class CancelRequested(Exception):
    """任务取消信号：由进度回调抛出，download_work 捕获后以 error=CancelRequested 结束。"""


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class TaskStore:
    """任务存储：内存 dict + tasks.json 落盘（写时加锁、原子替换）。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.tasks = {}  # id -> task dict
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            tasks = data.get("tasks", []) if isinstance(data, dict) else data
            if isinstance(tasks, list):
                for t in tasks:
                    if isinstance(t, dict) and t.get("id"):
                        # 服务重启后无对应下载线程：running 恢复为 queued，由 TaskRunner 重新入队
                        if t.get("status") == "running":
                            t["status"] = "queued"
                            t["error"] = "服务重启，任务恢复为排队状态"
                        self.tasks[t["id"]] = t
        except Exception as e:  # noqa: BLE001
            print(f"[server] 读取 tasks.json 失败（忽略）: {e}", flush=True)

    def persist(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps({"tasks": list(self.tasks.values())}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def get(self, task_id: str):
        return self.tasks.get(task_id)

    def all(self):
        return list(self.tasks.values())

    def put(self, task: dict) -> None:
        self.tasks[task["id"]] = task

    def update(self, task: dict, **fields) -> None:
        task.update(fields)
        self.persist()

    def delete(self, task_id: str) -> bool:
        with self._lock:
            if task_id not in self.tasks:
                return False
            del self.tasks[task_id]
        self.persist()
        return True


class TaskRunner:
    """任务调度：进程内线程池并发执行 download_work，进度回调更新任务状态并持久化。"""

    def __init__(self, store: TaskStore, workers: int = DEFAULT_WORKERS, default_output: str = ""):
        self.store = store
        self.default_output = default_output
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dl-task")
        self._active = set()      # 当前有下载线程的任务 id（进程内）
        self._active_lock = threading.Lock()
        # 服务重启恢复：把磁盘中 queued 任务（含由 running 恢复来的）重新入队
        for task in self.store.all():
            if task.get("status") == "queued":
                self.submit(task)

    def submit(self, task: dict) -> None:
        self.store.put(task)
        self.store.persist()
        with self._active_lock:
            self._active.add(task["id"])
        self.pool.submit(self._run, task["id"])

    def _run(self, task_id: str) -> None:
        try:
            self._run_inner(task_id)
        finally:
            with self._active_lock:
                self._active.discard(task_id)

    def _run_inner(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if not task:
            return
        # 入队后若已被取消（cancel 直接置 cancelled），不再启动下载
        if task.get("cancel_requested"):
            self.store.update(task, status="cancelled", error="已取消",
                              finished_at=now_str(), speed_kbps=0)
            return
        task["status"] = "running"
        task["started_at"] = now_str()
        task["error"] = ""
        task["cancel_requested"] = False
        self.store.persist()
        start_wall = time.monotonic()
        bytes_done = [0]

        def on_progress(stage, data):
            if task["cancel_requested"]:
                raise CancelRequested("用户取消")
            if stage == "meta":
                self.store.update(task, rj=data.get("rj", task.get("rj", "")),
                                  title=data.get("title", task.get("title", "")))
            elif stage == "filter":
                selected = data.get("selected", 0)
                self.store.update(task, files_total=selected, files_done=0,
                                  files_ok=0, files_skip=0, files_fail=0, progress=0.0)
            elif stage == "file":
                files_total = max(task.get("files_total", 0), 1)
                done = data.get("index", 0)
                ok = data.get("ok", False)
                skip = bool(data.get("skip", False))
                files_ok = task.get("files_ok", 0) + (1 if ok and not skip else 0)
                files_skip = task.get("files_skip", 0) + (1 if skip else 0)
                files_fail = task.get("files_fail", 0) + (0 if ok else 1)
                # 速度：以已完成文件大小 / 耗时估算（文件级粒度）
                try:
                    size = os.path.getsize(data["path"])
                    bytes_done[0] += size
                except OSError:
                    pass
                elapsed = max(time.monotonic() - start_wall, 0.01)
                self.store.update(
                    task, files_done=done, files_ok=files_ok, files_skip=files_skip,
                    files_fail=files_fail,
                    progress=round(done / files_total * 100, 1),
                    speed_kbps=round(bytes_done[0] / elapsed / 1024, 1),
                )
            elif stage == "done":
                self.store.persist()

        opts = task.get("options", {})
        try:
            stats = ad.download_work(
                task.get("url", ""),
                output_dir=opts.get("output", "") or str(Path(os.getcwd()) / "download"),
                types=opts.get("types", ""),
                exts=opts.get("ext", ""),
                exclude_exts=opts.get("exclude_ext", ""),
                include_regex=opts.get("include_regex", ""),
                exclude_regex=opts.get("exclude_regex", ""),
                workers=int(opts.get("workers", ad.DEFAULT_WORKERS)),
                retries=int(opts.get("retries", ad.DEFAULT_RETRIES)),
                min_speed=float(opts.get("min_speed", ad.DEFAULT_MIN_SPEED)),
                stall_time=float(opts.get("stall_time", ad.DEFAULT_STALL_TIME)),
                preallocate=bool(opts.get("preallocate", True)),
                on_progress=on_progress,
                cancel_cb=lambda: task.get("cancel_requested", False),
            )
        except ad.DownloadCancelled as e:
            self.store.update(task, status="cancelled", error=f"已取消: {e}",
                              finished_at=now_str(), speed_kbps=0)
            self.store.persist()
            return
        except CancelRequested as e:
            self.store.update(task, status="cancelled", error=f"已取消: {e}",
                              finished_at=now_str(), speed_kbps=0)
            self.store.persist()
            return
        except Exception as e:  # noqa: BLE001
            self.store.update(task, status="failed", error=f"{type(e).__name__}: {e}",
                              finished_at=now_str(), speed_kbps=0)
            self.store.persist()
            return

        err = stats.get("error", "")
        if err and err.startswith("CancelRequested"):
            self.store.update(task, status="cancelled", error="已取消",
                              finished_at=now_str(), speed_kbps=0)
        elif stats.get("fail", 0) > 0:
            failed = stats.get("failed_files", [])
            detail = "; ".join(
                f"{Path(f.get('path')).name}: {f.get('reason')}" if isinstance(f, dict) else str(f)
                for f in failed[:3]
            ) or "下载失败"
            self.store.update(task, status="failed",
                              error=detail,
                              files_fail=stats.get("fail", 0),
                              finished_at=now_str(), speed_kbps=0)
        else:
            self.store.update(task, status="done",
                              files_total=stats.get("total", 0),
                              files_done=stats.get("total", 0),
                              files_ok=stats.get("success", 0),
                              files_skip=stats.get("skip", 0),
                              files_fail=0, progress=100.0,
                              out_dir=stats.get("out_dir", ""),
                              finished_at=now_str(), speed_kbps=0)
        self.store.persist()

    def create(self, url: str, options: dict) -> dict:
        options = dict(options or {})
        if self.default_output and not options.get("output"):
            options["output"] = self.default_output
        # 重复任务合并：同 url / 同 rj 的旧任务复用同一条历史记录；运行中/排队中则提示已存在
        existing = self._find_existing(url)
        if existing:
            if existing["status"] in ("running", "queued"):
                existing["duplicate"] = True
                return existing
            # 复用旧记录重跑，保留同一条 id / 历史记录
            self._reset_for_run(existing, url=url, options=options)
            self.submit(existing)
            return existing
        task = {
            "id": uuid.uuid4().hex,
            "url": url,
            "rj": "",
            "title": url,
            "status": "queued",
            "progress": 0.0,
            "files_total": 0,
            "files_done": 0,
            "files_ok": 0,
            "files_skip": 0,
            "files_fail": 0,
            "speed_kbps": 0,
            "out_dir": "",
            "error": "",
            "options": options or {},
            "created_at": now_str(),
            "started_at": "",
            "finished_at": "",
            "retried_from": "",
        }
        self.submit(task)
        return task

    def _find_existing(self, url: str):
        """按 url 或 RJ 号查找已有任务（最新优先）。"""
        rj = ""
        try:
            rj = f"RJ{ad.extract_rj_number(url)}"
        except Exception:  # noqa: BLE001
            pass
        for task in reversed(self.store.all()):
            if rj and task.get("rj") == rj:
                return task
            if task.get("url") == url:
                return task
        return None

    @staticmethod
    def _reset_for_run(task: dict, url: str = "", options=None) -> None:
        """将旧任务记录重置为可重跑状态（保留 id / created_at 等历史字段）。"""
        if url:
            task["url"] = url
        if options is not None:
            task["options"] = dict(options or {})
        task["status"] = "queued"
        task["progress"] = 0.0
        task["files_total"] = 0
        task["files_done"] = 0
        task["files_ok"] = 0
        task["files_skip"] = 0
        task["files_fail"] = 0
        task["speed_kbps"] = 0
        task["error"] = ""
        task["started_at"] = ""
        task["finished_at"] = ""
        task.pop("cancel_requested", None)
        task.pop("duplicate", None)

    def retry(self, task_id: str) -> bool:
        task = self.store.get(task_id)
        if not task:
            return False
        if task["status"] in ("running", "queued"):
            return False
        # 合并回原任务记录（不新建 id）：复用原配置与 URL，断点续传跳过已完成文件
        self._reset_for_run(task, url=task.get("url", ""), options=task.get("options", {}))
        self.submit(task)
        return True

    def delete(self, task_id: str) -> bool:
        task = self.store.get(task_id)
        if not task:
            return False
        if task["status"] in ("running", "queued"):
            return False
        return self.store.delete(task_id)

    def cancel(self, task_id: str) -> bool:
        task = self.store.get(task_id)
        if not task:
            return False
        if task["status"] == "queued":
            self.store.update(task, status="cancelled", error="已取消",
                              finished_at=now_str())
            return True
        if task["status"] == "running":
            with self._active_lock:
                has_thread = task_id in self._active
            if not has_thread:
                # 无对应下载线程（如磁盘恢复残留的 running 幽灵状态）：直接置为已取消
                self.store.update(task, status="cancelled", error="已取消（无运行线程）",
                                  finished_at=now_str(), speed_kbps=0)
                return True
            task["cancel_requested"] = True
            self.store.persist()
            # 立即关闭引擎全部连接（含正在阻塞 recv 的），使下载线程尽快收到读异常退出
            try:
                ad.force_close_connections()
            except Exception:
                pass
            return True
        return False

    def clear_done(self) -> int:
        """批量删除所有已完成（done）任务，返回删除数量；不影响 running/queued。"""
        deleted = 0
        for task in self.store.all():
            if task.get("status") == "done" and self.delete(task["id"]):
                deleted += 1
        return deleted

    def retry_failed(self) -> int:
        """批量重试所有失败（failed）任务，返回重试数量；复用 retry 合并逻辑。"""
        retried = 0
        for task in self.store.all():
            if task.get("status") == "failed" and self.retry(task["id"]):
                retried += 1
        return retried


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AsmrDownloaderServer/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _send_file(self, target: Path, ctype: str) -> None:
        """统一文件响应：支持单段 Range（206+Content-Range）与 HEAD（只发头不发 body）。"""
        size = target.stat().st_size
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
            if not m:
                self.send_error(416, "Range Not Satisfiable")
                return
            a, b = m.group(1), m.group(2)
            if a == "" and b == "":
                self.send_error(416, "Range Not Satisfiable")
                return
            if a == "":
                # 后缀范围：bytes=-N 表示最后 N 字节
                suffix = int(b)
                if suffix <= 0 or suffix > size:
                    self.send_error(416, "Range Not Satisfiable")
                    return
                start, end = size - suffix, size - 1
            else:
                start = int(a)
                end = int(b) if b else size - 1
                if start >= size:
                    self.send_error(416, "Range Not Satisfiable")
                    return
                end = min(end, size - 1)
            length = end - start + 1
            self.send_response(206)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(target, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return
        self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if self.command != "HEAD":
            with open(target, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 64 * 1024)

    def _serve_static(self, rel: str) -> None:
        rel = rel.lstrip("/")
        if not rel or rel.endswith("/"):
            rel = "index.html"
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())):
            self.send_error(403, "Forbidden")
            return
        if not target.is_file():
            self.send_error(404, "Not Found")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send_file(target, ctype)

    def _serve_work_file(self, rel: str) -> None:
        """服务 /files/ 前缀的下载根目录静态文件（封面/音频/图片/文本等）。"""
        rel = unquote(rel).lstrip("/")
        target = (DOWNLOAD_ROOT / rel).resolve()
        root_resolved = DOWNLOAD_ROOT.resolve()
        if not str(target).startswith(str(root_resolved)) or ".." in rel.split("/"):
            self.send_error(403, "Forbidden")
            return
        if not target.is_file():
            self.send_error(404, "Not Found")
            return
        ctype = STATIC_CONTENT_TYPES.get(target.suffix.lower())
        if not ctype:
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send_file(target, ctype)

    def _serve_library(self) -> None:
        """GET /api/library：扫描下载根目录，返回作品库摘要列表。"""
        works = [build_work_summary(d) for d in iter_work_dirs(DOWNLOAD_ROOT)]
        works.sort(key=lambda w: (w.get("meta") or {}).get("release_date", "") or w.get("title", ""), reverse=True)
        self._send_json(200, {"works": works})

    def _serve_work(self, rj: str) -> None:
        """GET /api/work/{rj}：返回作品 meta 与递归文件列表。"""
        work_dir = resolve_work_dir(rj)
        if not work_dir:
            self._send_json(404, {"error": f"作品不存在: {rj}"})
            return
        meta = load_work_meta(work_dir)
        summary = build_work_summary(work_dir)
        payload = {
            "rj": summary["rj"],
            "title": summary["title"],
            "cover_url": summary["cover_url"],
            "dir": work_dir.name,
            "meta": meta,
            "files": list_work_files(work_dir),
        }
        self._send_json(200, payload)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        srv: "Server" = self.server
        if path == "/api/tasks":
            self._send_json(200, {"tasks": srv.runner.store.all()})
        elif path == "/api/library":
            self._serve_library()
        elif path.startswith("/api/work/"):
            self._serve_work(path[len("/api/work/"):])
        elif path.startswith("/api/tasks/"):
            task_id = path[len("/api/tasks/"):]
            task = srv.runner.store.get(task_id)
            if task:
                self._send_json(200, task)
            else:
                self._send_json(404, {"error": "任务不存在"})
        elif path.startswith("/files/"):
            self._serve_work_file(path[len("/files/"):])
        elif path.startswith("/api/"):
            self._send_json(404, {"error": "未知 API"})
        else:
            self._serve_static(path)

    def do_HEAD(self):
        # 与 GET 同路由；文件响应在 _send_file 内按 command == HEAD 只发头不发 body
        self.do_GET()

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        srv: "Server" = self.server
        body = self._read_json_body()
        if path == "/api/tasks":
            urls = body.get("urls") or body.get("url") or []
            if isinstance(urls, str):
                urls = [urls]
            urls = [u.strip() for u in urls if u and u.strip()]
            if not urls:
                self._send_json(400, {"error": "缺少下载地址（urls）"})
                return
            options = body.get("options", {})
            tasks = [srv.runner.create(u, options) for u in urls]
            self._send_json(200, {"tasks": tasks})
        elif path == "/api/tasks/retry-failed":
            retried = srv.runner.retry_failed()
            self._send_json(200, {"retried": retried})
        elif path.startswith("/api/tasks/"):
            rest = path[len("/api/tasks/"):]
            parts = rest.split("/")
            task_id = parts[0]
            action = parts[1] if len(parts) > 1 else ""
            if action == "retry":
                ok = srv.runner.retry(task_id)
                if ok:
                    self._send_json(200, {"ok": True})
                else:
                    self._send_json(400, {"error": "任务不存在或当前状态不可重试"})
            elif action == "cancel":
                ok = srv.runner.cancel(task_id)
                if ok:
                    self._send_json(200, {"ok": True})
                else:
                    self._send_json(400, {"error": "任务不存在或当前状态不可取消"})
            else:
                self._send_json(404, {"error": "未知操作"})
        else:
            self._send_json(404, {"error": "未知 API"})


    def do_DELETE(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        srv: "Server" = self.server
        if path == "/api/tasks/clear-done":
            deleted = srv.runner.clear_done()
            self._send_json(200, {"deleted": deleted})
        elif path.startswith("/api/tasks/"):
            task_id = path[len("/api/tasks/"):]
            if srv.runner.delete(task_id):
                self._send_json(200, {"ok": True})
            else:
                self._send_json(400, {"error": "任务不存在或当前状态不可删除（运行中/排队中不可删）"})
        else:
            self._send_json(404, {"error": "未知 API"})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, runner: TaskRunner):
        super().__init__(addr, Handler)
        self.runner = runner


def main() -> int:
    ap = argparse.ArgumentParser(prog="server", description="ASMR Downloader Web 管理端")
    ap.add_argument("--host", default=os.environ.get("ASMR_SERVER_HOST", DEFAULT_HOST))
    ap.add_argument("--port", type=int, default=int(os.environ.get("ASMR_SERVER_PORT", str(DEFAULT_PORT))))
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发下载任务数")
    ap.add_argument("--global-workers", type=int,
                    default=int(os.environ.get("ASMR_GLOBAL_WORKERS", str(ad.DEFAULT_GLOBAL_WORKERS))),
                    help="进程级全局下载线程上限（所有任务合计，默认 8）")
    ap.add_argument("--proxy", default="", help="HTTP/HTTPS 代理地址（默认读 ASMR_PROXY 环境变量，再读系统代理）")
    args = ap.parse_args()

    # 内建文件日志：控制台 + server.log 双写（UTF-8 追加），覆盖引擎 ad.log 与各任务线程输出
    log_path = install_file_logging()
    print(f"[server] 日志双写: {log_path}", flush=True)

    # 代理初始化：命令行 --proxy > ASMR_PROXY 环境变量 > 系统代理设置
    # 下载引擎默认直连，若本机需代理才能访问 API（如被墙），Web 端任务会一直卡在超时重试，
    # 表现为“下载没速度”。因此启动时同步 CLI 的代理配置。
    proxy = args.proxy or os.environ.get("ASMR_PROXY", "")
    if not proxy:
        try:
            import winreg
            _k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            if winreg.QueryValueEx(_k, "ProxyEnable")[0]:
                proxy = winreg.QueryValueEx(_k, "ProxyServer")[0]
        except Exception:
            proxy = ""
    if proxy and "://" not in proxy:
        proxy = "http://" + proxy
    if proxy:
        ad.setup_network(proxy)
        print(f"[server] 已启用代理: {proxy}", flush=True)

    store = TaskStore(TASKS_JSON)
    # 全局下载并发上限：限制所有任务合计的活跃下载线程数，避免资源耗尽
    ad.set_global_workers(args.global_workers)
    runner = TaskRunner(store, workers=args.workers)
    srv = Server((args.host, args.port), runner)
    print(f"[server] ASMR Downloader Web 管理端启动: http://{args.host}:{args.port}", flush=True)
    print(f"[server] 任务持久化: {TASKS_JSON}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] 停止。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
