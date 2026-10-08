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
- 失败重试：单文件默认重试 3 次（`--retries` 可调），带实时进度输出

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
```

## 参数说明

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `input` | ASMR 作品地址或 RJ 号（位置参数） | 必填 |
| `-o, --output` | 下载输出目录，作品会创建在该目录下；不指定时默认下载到当前工作目录下的 `download/` 子目录 | `download/`（当前目录下） |
| `--types` | 按 type 字段过滤，逗号分隔：`text,audio,image,other` | 不过滤 |
| `--ext` | 按扩展名白名单过滤，逗号分隔，如 `mp3,wav`；指定后默认排除 wav/flac 不生效 | 不过滤 |
| `--exclude-ext` | 排除扩展名，逗号分隔，如 `mp4,zip`；在默认排除 `wav/flac` 基础上追加 | 默认排除 `wav,flac` |
| `-w, --workers` | 并发下载线程数 | `8` |
| `-r, --retries` | 单文件失败重试次数 | `3` |

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
