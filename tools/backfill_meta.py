#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""为 download 下已下载但缺少 meta.json 的作品目录补写 meta.json + cover.jpg。

- 只补 meta 信息（复用 asmr_downloader._save_meta_and_cover），不重新下载作品文件；
- 单个作品抓取失败打印原因并继续处理下一个；
- 可复用：--root 指定下载根目录，--proxy 指定代理（默认读 ASMR_PROXY，再读系统代理）。

用法:
    python tools/backfill_meta.py [--root D:\\asmr-downloader\\download]
                                  [--proxy http://127.0.0.1:10808] [--dry-run]
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asmr_downloader as ad  # noqa: E402

_RJ_RE = re.compile(r"RJ(\d+)", re.IGNORECASE)
META_JSON = "meta.json"
COVER_JPG = "cover.jpg"


def resolve_proxy(cli_proxy: str) -> str:
    """命令行 --proxy > ASMR_PROXY 环境变量 > 系统代理设置（与 server.py 逻辑一致）。"""
    proxy = cli_proxy or os.environ.get("ASMR_PROXY", "")
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
    return proxy


def extract_rj(dir_name: str) -> str:
    """从目录名（RJxxxxx_标题）提取 RJ 数字标识；未命中返回空串。"""
    m = _RJ_RE.search(dir_name)
    return f"RJ{m.group(1)}" if m else ""


def scan_missing(root: Path, fill_cover: bool = False):
    """返回需要处理的顶层作品目录列表（目录名含 RJ 号）。

    fill_cover=False：仅缺 meta.json 的目录；
    fill_cover=True ：缺 meta.json，或已有 meta.json 但缺 cover.jpg 的目录。
    """
    missing = []
    if not root.is_dir():
        return missing
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        if not extract_rj(child.name):
            continue
        if not (child / META_JSON).is_file():
            missing.append(child)
            continue
        if fill_cover and not (child / COVER_JPG).is_file():
            missing.append(child)
    return missing


def backfill_one(work_dir: Path, fill_cover: bool = False) -> dict:
    """为单个作品目录补 meta.json + cover.jpg；失败打印原因并返回结果，不抛异常。

    fill_cover=True 且已有 meta.json 时，读取本地 meta 作为 work_info（仅补封面，
    不重复请求 API、不覆盖已有 meta 内容）；否则抓取 API 并落盘 meta+封面。
    """
    rj = extract_rj(work_dir.name)
    result = {"dir": work_dir.name, "rj": rj, "status": "pending",
              "reason": "", "meta_json": (work_dir / META_JSON).is_file(),
              "cover_jpg": (work_dir / COVER_JPG).is_file()}
    if fill_cover and result["meta_json"]:
        try:
            work_info = json.loads((work_dir / META_JSON).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            result["status"] = "failed"
            result["reason"] = f"读取本地 meta.json 失败: {type(e).__name__}: {e}"
            ad.log(f"[backfill] 失败 {work_dir.name}: {result['reason']}（继续下一个）")
            return result
    else:
        try:
            # fetch_work_info 接受数字串（内部自动处理前导零候选）；返回 (data, rj_used)
            work_info, _ = ad.fetch_work_info(rj.lstrip("RJ"))
        except Exception as e:  # noqa: BLE001
            result["status"] = "failed"
            result["reason"] = f"{type(e).__name__}: {e}"
            ad.log(f"[backfill] 失败 {work_dir.name}: {result['reason']}（继续下一个）")
            return result
    try:
        ad._save_meta_and_cover(work_dir, work_info)
    except Exception as e:  # noqa: BLE001
        result["status"] = "failed"
        result["reason"] = f"落盘 meta/封面失败: {type(e).__name__}: {e}"
        ad.log(f"[backfill] 失败 {work_dir.name}: {result['reason']}（继续下一个）")
        return result
    result["meta_json"] = (work_dir / META_JSON).is_file()
    result["cover_jpg"] = (work_dir / COVER_JPG).is_file()
    result["status"] = "ok" if (result["meta_json"] and result["cover_jpg"]) else "warn"
    if result["status"] == "warn":
        result["reason"] = "cover.jpg 未生成（不阻塞，见引擎日志）"
    return result


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="backfill_meta",
        description="为已下载作品补写 meta.json + cover.jpg（不重新下载）")
    ap.add_argument("--root", default=str(Path(os.getcwd()) / "download"),
                    help="下载根目录（默认 cwd/download）")
    ap.add_argument("--proxy", default="",
                    help="HTTP/HTTPS 代理地址（默认读 ASMR_PROXY，再读系统代理）")
    ap.add_argument("--dry-run", action="store_true", help="仅列出将处理的作品，不实际抓取")
    ap.add_argument("--fill-cover", action="store_true",
                    help="同时为已有 meta.json 但缺 cover.jpg 的作品补封面（读本地 meta，不重复抓 API）")
    args = ap.parse_args()

    root = Path(args.root)
    proxy = resolve_proxy(args.proxy)
    if proxy:
        ad.setup_network(proxy)
        print(f"[backfill] 已启用代理: {proxy}", flush=True)
    else:
        ad.setup_network("")

    missing = scan_missing(root, fill_cover=args.fill_cover)
    mode = "（含补封面）" if args.fill_cover else ""
    print(f"[backfill] 扫描 {root}：共 {len(missing)} 个作品目录需处理{mode}", flush=True)
    if not missing:
        return 0

    ok_count = failed_count = 0
    for work_dir in missing:
        if args.dry_run:
            print(f"[backfill][dry-run] 将处理 {work_dir.name}（rj={extract_rj(work_dir.name)}）", flush=True)
            continue
        res = backfill_one(work_dir, fill_cover=args.fill_cover)
        mark = {"ok": "OK", "warn": "WARN", "failed": "FAIL"}.get(res["status"], "FAIL")
        line = (f"[backfill][{mark}] {res['dir']}: rj={res['rj']} "
                f"meta={res['meta_json']} cover={res['cover_jpg']}")
        if res["reason"]:
            line += f" | {res['reason']}"
        print(line, flush=True)
        if res["status"] == "failed":
            failed_count += 1
        else:
            ok_count += 1

    print(f"[backfill] 完成：成功 {ok_count}，失败 {failed_count}（共 {len(missing)}）", flush=True)
    return 1 if failed_count else 0


if __name__ == "__main__":
    sys.exit(main())
