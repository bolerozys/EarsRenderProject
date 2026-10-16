# -*- coding: utf-8 -*-
"""PDF 发票转图片 —— 核心转换引擎。

本模块不导入任何 GUI 库，因此 Windows / macOS / Linux 通用，
GUI、命令行和自动化测试都复用它。

依赖
----
* PyMuPDF：解析 PDF 并渲染页面位图
* Pillow  ：保存 PNG / JPG / TIFF / WebP，并写入 DPI 元数据

设计要点
--------
* 多线程按“页”并行渲染，一个 PDF 的页面不会阻塞其它 PDF；
* 每个线程持有自己的 ``fitz.Document``（PyMuPDF 的文档对象不是线程安全的）；
* 支持取消、进度回调、页码范围、文件名模板、同名文件自动改名；
* 输出目录为空时默认使用“当前工作目录”。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

try:  # PyMuPDF >= 1.24 同时提供 `pymupdf` 与旧的 `fitz` 入口
    import pymupdf as fitz
except ImportError:  # pragma: no cover - 兼容老版本 PyMuPDF
    import fitz  # type: ignore[no-redef]

try:
    from PIL import Image
except ImportError:  # pragma: no cover - 在缺依赖时给出友好提示
    Image = None  # type: ignore[assignment]

__all__ = [
    "PDF_SUFFIX",
    "IMAGE_FORMATS",
    "DEFAULT_FORMAT",
    "DEFAULT_DPI",
    "DEFAULT_QUALITY",
    "DEFAULT_TEMPLATE",
    "ConversionError",
    "PasswordRequired",
    "ConvertOptions",
    "Progress",
    "FileOutcome",
    "ConversionReport",
    "PdfInfo",
    "parse_page_spec",
    "validate_template",
    "render_name",
    "sanitize_filename",
    "natural_key",
    "iter_pdf_inputs",
    "describe_pdf",
    "convert_files",
    "open_path",
    "default_output_dir",
    "check_dependencies",
]

PDF_SUFFIX = ".pdf"

#: 界面里可选的输出格式（取值为内部规范名）
IMAGE_FORMATS: dict[str, str] = {
    "png": "PNG（无损，画质最好）",
    "jpg": "JPG（体积小，适合存档/上传）",
    "tiff": "TIFF（无损，印刷/归档）",
    "webp": "WebP（体积小；质量 100 时为无损）",
}

# 默认值一律取“画质最好”的组合：无损 PNG + 300 DPI + 质量 100
DEFAULT_FORMAT = "png"
DEFAULT_DPI = 300
DEFAULT_QUALITY = 100
DEFAULT_TEMPLATE = "{name}_p{page:03d}"

#: 单页渲染的像素上限，超过则报错而不是把内存吃满
MAX_PIXELS = 200_000_000
#: DPI 允许范围
MIN_DPI, MAX_DPI = 36, 1200


class ConversionError(Exception):
    """转换过程中的可预期错误（会在界面上原样展示）。"""


class PasswordRequired(ConversionError):
    """PDF 已加密且密码缺失或错误。"""


# --------------------------------------------------------------------------- #
# 选项与结果数据结构
# --------------------------------------------------------------------------- #
@dataclass
class ConvertOptions:
    """一次批量转换的全部参数。"""

    #: 导出目录；为 None 或空时使用“当前工作目录”
    output_dir: Path | str | None = None
    #: png / jpg / tiff / webp
    image_format: str = DEFAULT_FORMAT
    #: 渲染分辨率
    dpi: int = DEFAULT_DPI
    #: JPG / WebP 质量（1-100；100 时 WebP 走无损）
    quality: int = DEFAULT_QUALITY
    #: 页码范围，如 "1-3,5,8-"；空字符串表示全部页面
    page_range: str = ""
    #: 文件名模板，可用 {name} {stem} {page} {page0} {total} {ext} {dpi}
    name_template: str = DEFAULT_TEMPLATE
    #: 已存在同名文件时是否覆盖
    overwrite: bool = False
    #: 输出灰度图（发票通常足够，体积更小）
    grayscale: bool = False
    #: 每个 PDF 建一个同名子文件夹
    subfolder_per_pdf: bool = False
    #: 加密 PDF 的密码
    password: str | None = None
    #: 并行线程数；None 表示自动
    workers: int | None = None

    def resolved_output_dir(self) -> Path:
        """返回实际导出目录：未设置时就是当前工作目录（不可写则退回桌面/主目录）。"""
        raw = self.output_dir
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return default_output_dir()
        return Path(raw).expanduser()

    @property
    def ext(self) -> str:
        fmt = (self.image_format or DEFAULT_FORMAT).lower().lstrip(".")
        fmt = {"jpeg": "jpg", "tif": "tiff"}.get(fmt, fmt)
        if fmt not in IMAGE_FORMATS:
            raise ConversionError(f"不支持的图片格式：{self.image_format}")
        return fmt

    def normalized_dpi(self) -> int:
        try:
            dpi = int(round(float(self.dpi)))
        except (TypeError, ValueError):
            raise ConversionError(f"DPI 必须是数字，当前为：{self.dpi!r}")
        if not (MIN_DPI <= dpi <= MAX_DPI):
            raise ConversionError(f"DPI 需要在 {MIN_DPI}-{MAX_DPI} 之间，当前为 {dpi}")
        return dpi


@dataclass
class Progress:
    """进度回调携带的事件。"""

    done: int
    total: int
    message: str
    path: Path | None = None
    #: start / page / file_done / file_failed
    event: str = "page"

    @property
    def ratio(self) -> float:
        return (self.done / self.total) if self.total else 0.0


@dataclass
class FileOutcome:
    """单个 PDF 的转换结果。"""

    path: Path
    pages: int = 0
    written: int = 0
    outputs: list[Path] = field(default_factory=list)
    error: str | None = None
    #: ok / failed / skipped
    status: str = "skipped"

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class ConversionReport:
    """整批任务的结果汇总。"""

    files: list[FileOutcome] = field(default_factory=list)
    output_dir: Path = field(default_factory=Path.cwd)
    cancelled: bool = False
    elapsed: float = 0.0
    #: 不是 PDF 而被忽略的输入
    ignored: list[Path] = field(default_factory=list)

    @property
    def outputs(self) -> list[Path]:
        return [p for f in self.files for p in f.outputs]

    @property
    def failures(self) -> list[FileOutcome]:
        return [f for f in self.files if f.status == "failed"]

    @property
    def succeeded(self) -> list[FileOutcome]:
        return [f for f in self.files if f.status == "ok"]

    @property
    def total_pages(self) -> int:
        return sum(f.pages for f in self.files)

    @property
    def done_pages(self) -> int:
        return sum(f.written for f in self.files)

    def summary(self) -> str:
        """一行中文摘要，供界面和命令行显示。"""
        parts = [
            f"成功 {len(self.succeeded)} 个文件 / {self.done_pages} 页",
        ]
        if self.failures:
            parts.append(f"失败 {len(self.failures)} 个文件")
        if self.ignored:
            parts.append(f"忽略 {len(self.ignored)} 个非 PDF 文件")
        if self.cancelled:
            parts.append("已取消")
        parts.append(f"耗时 {self.elapsed:.1f} 秒")
        return "，".join(parts)


@dataclass
class PdfInfo:
    """打开 PDF 后的基本信息。"""

    path: Path
    pages: int = 0
    encrypted: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.pages > 0


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def default_output_dir() -> Path:
    """未指定导出目录时使用的目录：**当前工作目录**。

    从文件管理器双击启动时当前目录可能不可写（macOS 的 .app 被 Finder 启动时
    当前目录是 ``/``，Windows 装在 Program Files 下同理），这时退回桌面/主目录，
    避免"默认导出到当前目录"直接失败。
    """
    cwd = Path.cwd()
    try:
        if os.access(cwd, os.W_OK):
            return cwd
    except OSError:  # pragma: no cover - 极少数异常路径
        pass
    for fallback in (Path.home() / "Desktop", Path.home()):
        if fallback.is_dir() and os.access(fallback, os.W_OK):
            return fallback
    return Path.home()


def open_path(path: Path | str) -> None:
    """用系统文件管理器打开目录/文件（Windows / macOS / Linux）。"""
    target = str(path)
    if sys.platform == "darwin":
        subprocess.Popen(["open", target])
    elif os.name == "nt":
        os.startfile(target)  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", target])


def natural_key(value: str | Path) -> list:
    """自然排序：``发票2.pdf`` 排在 ``发票10.pdf`` 前面。"""
    text = str(value)
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", text)]


_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, max_length: int = 180) -> str:
    """把任意字符串清理成 Windows / macOS 都合法的文件名（不含扩展名）。"""
    cleaned = _ILLEGAL_CHARS.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(" .")  # Windows 不允许结尾的点或空格
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    return cleaned or "output"


def parse_page_spec(spec: str | None, total: int) -> list[int]:
    """解析页码范围字符串，返回 1 起始的页码列表。

    支持 ``"1-3,5,8-"``、中文逗号、``至``/``~`` 分隔符；空值表示全部页面。
    超出范围的页码会被忽略；若一个有效页码都没有则抛 :class:`ConversionError`。
    """
    if total <= 0:
        return []
    text = (spec or "").strip()
    if not text:
        return list(range(1, total + 1))

    pages: list[int] = []
    for chunk in re.split(r"[,，;；\s]+", text):
        if not chunk:
            continue
        ranged = re.fullmatch(r"(\d+)\s*[-–—~～至]\s*(\d*)", chunk)
        if ranged:
            start = int(ranged.group(1))
            end = int(ranged.group(2)) if ranged.group(2) else total
        elif chunk.isdigit():
            start = end = int(chunk)
        else:
            raise ConversionError(f"无法识别的页码范围片段：{chunk!r}（示例：1-3,5,8-）")
        if start > end:
            start, end = end, start
        pages.extend(range(start, end + 1))

    valid = sorted({p for p in pages if 1 <= p <= total})
    if not valid:
        raise ConversionError(f"页码范围 {text!r} 在该 PDF（共 {total} 页）中没有有效页面")
    return valid


_TEMPLATE_FIELDS = {"name", "stem", "page", "page0", "total", "ext", "dpi"}


def validate_template(template: str) -> str:
    """校验文件名模板，返回去掉首尾空白后的模板。"""
    text = (template or "").strip()
    if not text:
        raise ConversionError("文件名模板不能为空")
    try:
        text.format(name="发票", stem="发票", page=1, page0=0, total=1, ext="png", dpi=200)
    except (KeyError, IndexError) as exc:
        raise ConversionError(
            f"文件名模板里的占位符 {{{exc.args[0]}}} 不受支持；"
            f"可用：{', '.join('{%s}' % f for f in sorted(_TEMPLATE_FIELDS))}"
        ) from None
    except ValueError as exc:
        raise ConversionError(f"文件名模板格式有误：{exc}") from None
    return text


def render_name(template: str, pdf: Path, page: int, total: int, ext: str, dpi: int) -> str:
    """按模板生成不带扩展名的文件名。"""
    stem = pdf.stem
    try:
        raw = template.format(
            name=stem, stem=stem, page=page, page0=page - 1, total=total, ext=ext, dpi=dpi
        )
    except Exception as exc:  # pragma: no cover - 已由 validate_template 兜住
        raise ConversionError(f"文件名模板渲染失败：{exc}") from None
    return sanitize_filename(raw)


def iter_pdf_inputs(paths: Iterable[Path | str], recursive: bool = True) -> tuple[list[Path], list[Path]]:
    """展开输入（文件或目录），返回 ``(pdf 列表, 被忽略的文件列表)``。"""
    pdfs: list[Path] = []
    ignored: list[Path] = []
    seen: set[str] = set()

    def add(candidate: Path) -> None:
        try:
            key = str(candidate.resolve()).casefold()
        except OSError:  # pragma: no cover - 极少数无权限路径
            key = str(candidate).casefold()
        if key in seen:
            return
        seen.add(key)
        if candidate.suffix.lower() == PDF_SUFFIX:
            pdfs.append(candidate)
        else:
            ignored.append(candidate)

    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            walker = path.rglob("*") if recursive else path.glob("*")
            for child in sorted(walker, key=natural_key):
                if child.is_file():
                    add(child)
        elif path.is_file():
            add(path)
        else:
            ignored.append(path)

    pdfs.sort(key=natural_key)
    return pdfs, ignored


# --------------------------------------------------------------------------- #
# 依赖检查
# --------------------------------------------------------------------------- #
def check_dependencies() -> list[str]:
    """返回缺失依赖的中文说明列表；为空表示依赖齐全。"""
    problems: list[str] = []
    try:
        fitz.version  # noqa: B018 - 触发属性访问以确认可用
    except Exception as exc:  # pragma: no cover
        problems.append(f"PyMuPDF 不可用：{exc}")
    if Image is None:
        problems.append("Pillow 不可用（用于保存图片），请安装：pip install Pillow")
    return problems


# --------------------------------------------------------------------------- #
# 打开 PDF 获取页数
# --------------------------------------------------------------------------- #
def describe_pdf(path: Path | str, password: str | None = None) -> PdfInfo:
    """打开 PDF 读取页数；失败时把原因写进 :attr:`PdfInfo.error`。"""
    pdf = Path(path)
    info = PdfInfo(path=pdf)
    try:
        doc = fitz.open(str(pdf))
    except Exception as exc:
        info.error = f"无法打开：{exc}"
        return info
    try:
        info.encrypted = bool(doc.needs_pass)
        if doc.needs_pass:
            if not doc.authenticate(password or ""):
                info.error = "文件已加密，需要密码"
                return info
        info.pages = int(doc.page_count)
        if info.pages <= 0:
            info.error = "该 PDF 没有任何页面"
    except Exception as exc:  # pragma: no cover - 损坏文件
        info.error = f"读取失败：{exc}"
    finally:
        try:
            doc.close()
        except Exception:  # pragma: no cover
            pass
    return info


# --------------------------------------------------------------------------- #
# 转换引擎
# --------------------------------------------------------------------------- #
class _DocPool:
    """线程私有的 ``fitz.Document`` 缓存。

    PyMuPDF 的 ``Document`` 不能跨线程共享，因此每个工作线程各自打开并缓存
    文档；任务结束后由主线程统一关闭。
    """

    def __init__(self, password: str | None = None) -> None:
        self._password = password
        self._local = threading.local()
        self._docs: list["fitz.Document"] = []
        self._lock = threading.Lock()

    def get(self, path: Path) -> "fitz.Document":
        cache: dict[str, "fitz.Document"] | None = getattr(self._local, "cache", None)
        if cache is None:
            cache = {}
            self._local.cache = cache
        key = str(path)
        doc = cache.get(key)
        if doc is None:
            doc = fitz.open(key)
            if doc.needs_pass:
                if not doc.authenticate(self._password or ""):
                    doc.close()
                    raise PasswordRequired(f"{path.name} 已加密，密码缺失或错误")
            cache[key] = doc
            with self._lock:
                self._docs.append(doc)
        return doc

    def close_all(self) -> None:
        with self._lock:
            docs, self._docs = self._docs, []
        for doc in docs:
            try:
                doc.close()
            except Exception:  # pragma: no cover
                pass


def _save_pixmap(
    pix: "fitz.Pixmap",
    target: Path,
    fmt: str,
    quality: int,
    dpi: int,
) -> None:
    """把渲染结果写盘（Pillow 负责编码与 DPI 元数据）。

    PNG / TIFF 本身就是无损；JPG 在质量 100 时关闭色度抽样；WebP 在质量 100 时
    改用无损编码，保证“默认即最高画质”。
    """
    if Image is None:  # pragma: no cover - 启动时已检查
        raise ConversionError("缺少 Pillow，无法保存图片")
    mode = "L" if pix.n == 1 else "RGB"
    image = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    try:
        params: dict = {"dpi": (dpi, dpi)}
        if fmt == "png":
            params["compress_level"] = 6  # 无损压缩，只影响体积不影响画质
        elif fmt == "jpg":
            params["quality"] = quality
            # 质量 >= 95 时不做色度抽样（4:4:4），避免细字/红线边缘发虚
            params["subsampling"] = 0 if quality >= 95 else 2
            params["optimize"] = quality >= 95
        elif fmt == "tiff":
            params["compression"] = "tiff_deflate"  # 无损
        elif fmt == "webp":
            if quality >= 100:
                params["lossless"] = True
                params["method"] = 6  # 无损下用最高压缩强度
            else:
                params["quality"] = quality
                params["method"] = 4
        image.save(str(target), format={"jpg": "JPEG", "tiff": "TIFF"}.get(fmt, fmt.upper()), **params)
    finally:
        image.close()


def _unique_path(target: Path, overwrite: bool, reserved: set[str]) -> Path:
    """处理同名文件：覆盖、或自动追加 (2)、(3)…"""
    key = str(target).casefold()
    if overwrite:
        reserved.add(key)
        return target
    if key not in reserved and not target.exists():
        reserved.add(key)
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    index = 2
    while True:
        candidate = parent / f"{stem} ({index}){suffix}"
        ckey = str(candidate).casefold()
        if ckey not in reserved and not candidate.exists():
            reserved.add(ckey)
            return candidate
        index += 1


def convert_files(
    inputs: Sequence[Path | str],
    options: ConvertOptions | None = None,
    on_progress: Callable[[Progress], None] | None = None,
    cancel_event: threading.Event | None = None,
    recursive: bool = True,
) -> ConversionReport:
    """把一批 PDF（或包含 PDF 的目录）批量转换成图片。

    参数
    ----
    inputs
        文件或目录路径列表；非 PDF 文件会被记入 :attr:`ConversionReport.ignored`。
    options
        转换参数，见 :class:`ConvertOptions`。
    on_progress
        进度回调，会在工作线程中被调用，界面层需要自己做线程安全处理。
    cancel_event
        置位后尽快停止（已完成的图片保留）。
    recursive
        输入目录时是否递归查找子目录。

    返回
    ----
    ConversionReport
    """
    opts = options or ConvertOptions()
    problems = check_dependencies()
    if problems:
        raise ConversionError("；".join(problems))

    dpi = opts.normalized_dpi()
    fmt = opts.ext
    template = validate_template(opts.name_template)
    quality = max(1, min(100, int(opts.quality or 90)))

    started = time.monotonic()
    out_dir = opts.resolved_output_dir()
    report = ConversionReport(output_dir=out_dir)

    pdfs, ignored = iter_pdf_inputs(inputs, recursive=recursive)
    report.ignored = ignored
    if not pdfs:
        report.elapsed = time.monotonic() - started
        return report

    # 1) 先探测每个文件的页数与可用性，顺便算出总页数
    plans: list[tuple[FileOutcome, list[int]]] = []
    for pdf in pdfs:
        outcome = FileOutcome(path=pdf)
        info = describe_pdf(pdf, opts.password)
        if not info.ok:
            outcome.status = "failed"
            outcome.error = info.error
            plans.append((outcome, []))
            continue
        outcome.pages = info.pages
        try:
            pages = parse_page_spec(opts.page_range, info.pages)
        except ConversionError as exc:
            outcome.status = "failed"
            outcome.error = str(exc)
            plans.append((outcome, []))
            continue
        plans.append((outcome, pages))
    report.files = [outcome for outcome, _ in plans]

    total_tasks = sum(len(pages) for _, pages in plans)
    if total_tasks == 0:
        report.elapsed = time.monotonic() - started
        return report

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConversionError(f"无法创建导出目录 {out_dir}：{exc}") from None

    workers = opts.workers or max(1, min(4, (os.cpu_count() or 2)))
    pool = _DocPool(opts.password)
    reserved: set[str] = set()
    lock = threading.Lock()
    cancelled = False
    done = 0

    def report_progress(message: str, path: Path | None = None, event: str = "page") -> None:
        if on_progress is None:
            return
        try:
            on_progress(Progress(done=done, total=total_tasks, message=message, path=path, event=event))
        except Exception:  # pragma: no cover - 回调异常不应影响转换
            pass

    def sub_dir_for(outcome: FileOutcome) -> Path:
        if not opts.subfolder_per_pdf:
            return out_dir
        folder = out_dir / sanitize_filename(outcome.path.stem, 80)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def render(outcome: FileOutcome, page: int) -> Path:
        doc = pool.get(outcome.path)
        pdf_page = doc.load_page(page - 1)
        # 先按页面尺寸估算像素数，避免超大页面把内存吃满
        scale = dpi / 72.0
        estimated = (pdf_page.rect.width * scale) * (pdf_page.rect.height * scale)
        if estimated > MAX_PIXELS:
            raise ConversionError(
                f"第 {page} 页在 {dpi} DPI 下约 {int(pdf_page.rect.width * scale)}×"
                f"{int(pdf_page.rect.height * scale)} 像素，超出安全上限，请降低 DPI"
            )
        colorspace = fitz.csGRAY if opts.grayscale else fitz.csRGB
        pix = pdf_page.get_pixmap(dpi=dpi, colorspace=colorspace, alpha=False)
        name = render_name(template, outcome.path, page, outcome.pages, fmt, dpi)
        with lock:
            target = _unique_path(sub_dir_for(outcome) / f"{name}.{fmt}", opts.overwrite, reserved)
        _save_pixmap(pix, target, fmt, quality, dpi)
        return target

    report_progress("开始转换…", event="start")

    tasks: deque[tuple[int, FileOutcome, int]] = deque(
        (index, outcome, page)
        for index, (outcome, pages) in enumerate(plans)
        for page in pages
        if outcome.status != "failed"
    )
    failed_indexes: set[int] = set()
    futures: dict = {}

    def handle_success(outcome: FileOutcome, page: int, target: Path) -> None:
        nonlocal done
        with lock:
            if outcome.status != "failed":
                outcome.status = "ok"
                outcome.written += 1
                outcome.outputs.append(target)
            done += 1
            current_done = done
        message = f"{outcome.name} 第 {page} 页 → {target.name}"
        if on_progress is not None:
            try:
                on_progress(
                    Progress(done=current_done, total=total_tasks, message=message,
                             path=outcome.path, event="page")
                )
            except Exception:  # pragma: no cover
                pass

    def handle_failure(index: int, outcome: FileOutcome, page: int, exc: BaseException) -> None:
        nonlocal done
        message = str(exc) or exc.__class__.__name__
        with lock:
            failed_indexes.add(index)
            if outcome.status != "failed":
                outcome.status = "failed"
                outcome.error = message if page == 0 else f"第 {page} 页：{message}"
            done += 1
            current_done = done
        if on_progress is not None:
            try:
                on_progress(
                    Progress(done=current_done, total=total_tasks, message=f"{outcome.name} 失败：{message}",
                             path=outcome.path, event="file_failed")
                )
            except Exception:  # pragma: no cover
                pass

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="render") as executor:
        while tasks or futures:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                tasks.clear()
            while tasks and len(futures) < workers * 2 and not cancelled:
                index, outcome, page = tasks.popleft()
                if index in failed_indexes:
                    done += 1
                    continue
                futures[executor.submit(render, outcome, page)] = (index, outcome, page)
            if not futures:
                break
            completed, _ = wait(list(futures), return_when=FIRST_COMPLETED)
            for future in completed:
                index, outcome, page = futures.pop(future)
                try:
                    target = future.result()
                except BaseException as exc:  # noqa: BLE001 - 单页失败不影响整批
                    handle_failure(index, outcome, page, exc)
                else:
                    handle_success(outcome, page, target)

    pool.close_all()

    # 2) 收尾：标记被取消或没有成功页面的文件
    for index, (outcome, pages) in enumerate(plans):
        if outcome.status == "failed":
            if index in failed_indexes and outcome.error is None:
                outcome.error = "转换失败"
            continue
        if cancelled:
            outcome.status = "ok" if outcome.written else "skipped"
            if not outcome.written:
                outcome.error = "已取消"
        elif outcome.written:
            outcome.status = "ok"
        elif pages:
            outcome.status = "failed"
            outcome.error = outcome.error or "没有生成任何图片"
        else:
            outcome.status = "skipped"

    report.cancelled = cancelled
    report.elapsed = time.monotonic() - started
    return report
