---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: d6171082142361079cc73f7fd6fe4793_fb11ba96c24611f18019525400248c00
    ReservedCode1: r4kl5mhzRqWugY98KBJgfeAOrR9TNCMR/IsjE+P2lcvpWqZhx8ex0a2Avf2UPEFukeKLeDk+7i+CPftXaLRVGq8xgJNdeVYGBadoubgXQ24WqZqq7pg/XGHp1s3n9UvFB1VnQ1v8b7OsCTqmQaFU1iz/6Z+n4/Xh7ojUsJMQFQWPpF8tZg/yjJ5iYlQ=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: d6171082142361079cc73f7fd6fe4793_fb11ba96c24611f18019525400248c00
    ReservedCode2: r4kl5mhzRqWugY98KBJgfeAOrR9TNCMR/IsjE+P2lcvpWqZhx8ex0a2Avf2UPEFukeKLeDk+7i+CPftXaLRVGq8xgJNdeVYGBadoubgXQ24WqZqq7pg/XGHp1s3n9UvFB1VnQ1v8b7OsCTqmQaFU1iz/6Z+n4/Xh7ojUsJMQFQWPpF8tZg/yjJ5iYlQ=
---

# asmr-downloader

ASMR 作品下载命令行工具。根据 ASMR 作品地址或 RJ 号，自动获取作品元数据与文件树，并并发下载到本地。

## 功能特性

- 输入 ASMR 地址（`https://asmr.one/work/RJ01310099?path=%5B%5D#work-tree`）或 RJ 号（`RJ01310099`），自动提取 RJ 数字号
- 调用 `https://api.asmr-200.com/api/workInfo/{rj_number}?v2` 获取作品元数据（title 等）
- 调用 `https://api.asmr-200.com/api/tracks/{rj_number}?v=2` 获取文件树，按 `children` 递归保持原始目录结构
- 输出目录命名规则：`RJ号_标题`（如 `RJ01310099_【简体中文版】…`），自动替换 Windows 文件名非法字符
- 未指定 `-o` 时默认下载到脚本执行目录（当前工作目录）下的 `download/` 子目录，运行前自动创建
- 文件类型过滤：
  - `--types` 按 type 字段过滤（text / audio / image / other）
  - `--ext` 按扩展名过滤（如 `mp3,wav`）
  - `--exclude-ext` 排除扩展名（如 `mp4,zip`）
  - 三种规则可任意组合
- 并发下载：`ThreadPoolExecutor` 默认 8 线程（`--workers` 可调）
- 断点续传：已存在且大小匹配的完整文件自动跳过；未完成的 `.part` 文件删除重下
- 失败重试：单文件默认重试 3 次（`--retries` 可调），带实时进度输出；HTTP 408/429/5xx 视为可重试，连接层故障自动重试并自适应读超时
- 批量下载：一次传入多个作品地址/RJ 号，多作品间错开启动、统一汇总统计（成功/跳过/失败）
- 连接复用：同源 HTTP 连接池（keep-alive），弱网与代理场景显著减少 TLS 重连
- 断点自愈：分片级断点续传，进程中断后重跑自动补齐缺失分片
- Web 下载管理端：浏览器可视化创建/跟踪/重试/取消下载任务（见下文「Web 下载管理端」）

## 环境要求

- Python 3.8+
- 仅标准库（urllib / json / threading），无需安装第三方包

## 使用说明

```bash
# 不指定输出目录：默认下载到当前目录下的 download/ 子目录
python asmr_downloader.py "https://asmr.one/work/RJ01310099?path=%5B%5D#work-tree"

# 指定输出目录：下载整个作品到 D:/Downloads
python asmr_downloader.py "https://asmr.one/work/RJ01310099?path=%5B%5D#work-tree" --output D:/Downloads

# 或直接使用 RJ 号
python asmr_downloader.py RJ01310099 --output D:/Downloads

# 只下载音频类型
python asmr_downloader.py RJ01310099 --types audio --output D:/Downloads

# 只下载 mp3/wav 文件
python asmr_downloader.py RJ01310099 --ext mp3,wav --output D:/Downloads

# 过滤 + 扩展名组合
python asmr_downloader.py RJ01310099 --types audio --ext mp3,wav --output D:/Downloads

# 排除视频与压缩包
python asmr_downloader.py RJ01310099 --exclude-ext mp4,zip --output D:/Downloads

# 调整并发与重试
python asmr_downloader.py RJ01310099 --workers 4 --retries 5 --output D:/Downloads

# 批量下载多个作品（地址与 RJ 号可混用）
python asmr_downloader.py RJ01310099 "https://asmr.one/work/RJ01310100?path=%5B%5D#work-tree" --output D:/Downloads
```

## 参数说明

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `input` | ASMR 作品地址或 RJ 号（位置参数，可传多个实现批量下载） | 必填 |
| `-o, --output` | 下载输出目录，作品会创建在该目录下；不指定时默认下载到当前工作目录下的 `download/` 子目录 | `download/`（当前目录下） |
| `--types` | 按 type 字段过滤，逗号分隔：`text,audio,image,other` | 不过滤 |
| `--ext` | 按扩展名白名单过滤，逗号分隔，如 `mp3,wav`；指定后默认排除 wav/flac/mp4 不生效 | 不过滤 |
| `--exclude-ext` | 排除扩展名，逗号分隔，如 `mp4,zip`；在默认排除基础上追加 | 默认排除 `wav,flac,mp4` |
| `--regex` | 正则表达式过滤文件名（匹配则保留），如 `Track` | 不过滤 |
| `--config` | 配置文件路径（JSON），可用键覆盖命令行默认值 | 无 |
| `--log` | 日志文件路径，追加带时间戳的详细日志 | 无 |
| `-w, --workers` | 并发下载线程数 | `8` |
| `-r, --retries` | 单文件失败重试次数 | `3` |
| `--proxy` | HTTP 代理地址，如 `http://127.0.0.1:10808` | 无 |

## Web 下载管理端

基于 Python 标准库（`ThreadingHTTPServer`）的本地 Web 服务，复用 CLI 同一套下载引擎（`asmr_downloader.py` 的 `download_work` 入口），通过浏览器即可创建下载任务、实时查看进度与历史记录、对失败任务一键重试、取消运行中任务。任务状态持久化到 `server/tasks.json`，服务重启后历史记录与未完成任务自动恢复。

### 启动方式

```bash
# 默认监听 127.0.0.1:8000，仅本机可访问
python server/server.py

# 指定端口 / 输出目录（可选项）
python server/server.py --port 9000 --output D:/Downloads
```

启动后浏览器打开 <http://127.0.0.1:8000> 即可使用（前端为 Vue 3 CDN 单文件页面，无需 Node 构建链）。并发任务数默认为 2，可用 `--workers` 调整。

### 功能

- 下载地址输入框：支持粘贴多个 ASMR 地址或 RJ 号（每行一个），点击「开始下载」批量创建任务
- 任务卡片：实时进度条、状态（排队/下载中/完成/失败/已取消）、速度、文件数、输出目录
- 历史记录：已完成/失败/已取消任务保留；失败任务带「重试」按钮，复用断点续传跳过已完成文件
- 取消任务：运行中/排队任务可取消

### API 列表

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/tasks` | 创建下载任务，body `{"urls": ["RJ01310099", "..."]}`，返回 `{tasks: [任务对象...]}` |
| `GET` | `/api/tasks` | 任务列表（含历史记录），返回 `{tasks: [任务对象...]}` |
| `GET` | `/api/tasks/<id>` | 单个任务进度详情（进度/状态/速度/文件数/错误等） |
| `POST` | `/api/tasks/<id>/retry` | 重试失败任务，复用断点续传 |
| `POST` | `/api/tasks/<id>/cancel` | 取消运行中/排队任务 |

任务对象字段：`id / url / rj / title / status(queued|running|done|failed|cancelled) / progress / files_total / files_done / files_ok / files_skip / files_fail / speed_kbps / out_dir / error / created_at / started_at / finished_at / retried_from / cancel_requested`。

### 文件结构

```
server/
├── server.py          # ThreadingHTTPServer 服务端：JSON API + 静态托管 + 任务线程池 + tasks.json 持久化
├── __init__.py        # 包标记（支持 python -m server.server）
├── tasks.json         # 任务状态持久化（运行时自动创建）
└── web/               # Vue 3 CDN 前端
    ├── index.html
    ├── style.css
    └── app.js
```

## 输出结构示例

```
downloads/
└── RJ01310099_【简体中文版】…/
    ├── 音频/
    │   └── 正片/
    │       └── Track1.mp3
    ├── 文本/
    │   └── 简介.txt
    └── 图片/
        └── cover.jpg
```

## 常见问题

- **无法解析 RJ 号**：输入中必须包含 `RJ` + 数字（不区分大小写）。
- **下载失败**：网络波动会自动重试；重试耗尽后会在日志中列出失败文件路径，可重新运行命令续下（完整文件会自动跳过）。
- **断点续传**：下载中进程中断会残留 `.part` 文件，下次运行会自动删除重下。
*（内容由AI生成，仅供参考）*
