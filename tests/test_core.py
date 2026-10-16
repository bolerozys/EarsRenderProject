# -*- coding: utf-8 -*-
"""核心引擎与命令行的自动化测试。

运行方式（Windows / macOS 相同）::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf as fitz  # noqa: E402
from PIL import Image  # noqa: E402

from invoice2img import cli  # noqa: E402
from invoice2img.core import (  # noqa: E402
    DEFAULT_DPI,
    DEFAULT_FORMAT,
    DEFAULT_QUALITY,
    ConversionError,
    ConvertOptions,
    convert_files,
    describe_pdf,
    iter_pdf_inputs,
    natural_key,
    parse_page_spec,
    render_name,
    sanitize_filename,
    validate_template,
)

A4 = (595.0, 842.0)


def make_pdf(path: Path, pages: int = 1, text: str = "INVOICE", encrypted: bool = False,
             size: tuple[float, float] = A4) -> Path:
    """生成一个测试用 PDF。"""
    doc = fitz.open()
    for index in range(pages):
        page = doc.new_page(width=size[0], height=size[1])
        page.insert_text((72, 120), f"{text} #{index + 1}", fontsize=22)
        page.insert_text((72, 160), "test invoice", fontsize=12)
    path.parent.mkdir(parents=True, exist_ok=True)
    if encrypted:
        doc.save(
            str(path),
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="owner-secret",
            user_pw="1234",
        )
    else:
        doc.save(str(path))
    doc.close()
    return path


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pdf2img-test-")
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "出图"
        self.addCleanup(self._tmp.cleanup)

    def read_image(self, path: Path) -> Image.Image:
        image = Image.open(path)
        image.load()
        return image


# --------------------------------------------------------------------------- #
# 纯工具函数
# --------------------------------------------------------------------------- #
class TestHelpers(TempDirCase):
    def test_parse_page_spec(self) -> None:
        self.assertEqual(parse_page_spec("", 3), [1, 2, 3])
        self.assertEqual(parse_page_spec(None, 3), [1, 2, 3])
        self.assertEqual(parse_page_spec("2", 5), [2])
        self.assertEqual(parse_page_spec("1-3", 5), [1, 2, 3])
        self.assertEqual(parse_page_spec("1-2,4，5", 6), [1, 2, 4, 5])
        self.assertEqual(parse_page_spec("3-", 4), [3, 4])
        self.assertEqual(parse_page_spec("2至4", 6), [2, 3, 4])
        self.assertEqual(parse_page_spec("5-2", 6), [2, 3, 4, 5])
        with self.assertRaises(ConversionError):
            parse_page_spec("9-12", 3)  # 全部越界 -> 明确报错
        with self.assertRaises(ConversionError):
            parse_page_spec("abc", 3)

    def test_parse_page_spec_out_of_range_raises_in_convert(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf", pages=1)
        report = convert_files([pdf], ConvertOptions(output_dir=self.out, page_range="5-9"))
        self.assertEqual(report.files[0].status, "failed")
        self.assertIn("没有有效页面", report.files[0].error or "")

    def test_sanitize_filename(self) -> None:
        self.assertEqual(sanitize_filename('a<b>c:d"e/f\\g|h?i*j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(sanitize_filename("  发票   001  "), "发票 001")
        self.assertEqual(sanitize_filename("trailing. "), "trailing")
        self.assertEqual(sanitize_filename(""), "output")
        self.assertLessEqual(len(sanitize_filename("x" * 500)), 180)

    def test_natural_key(self) -> None:
        names = ["发票10.pdf", "发票2.pdf", "发票1.pdf"]
        self.assertEqual(sorted(names, key=natural_key), ["发票1.pdf", "发票2.pdf", "发票10.pdf"])

    def test_validate_template(self) -> None:
        self.assertEqual(validate_template("{name}-{page}"), "{name}-{page}")
        with self.assertRaises(ConversionError):
            validate_template("{unknown}")
        with self.assertRaises(ConversionError):
            validate_template("   ")

    def test_render_name(self) -> None:
        pdf = Path("/tmp/增值税发票 001.pdf")
        self.assertEqual(
            render_name("{name}_p{page:03d}", pdf, 7, 10, "png", 200), "增值税发票 001_p007"
        )
        self.assertEqual(render_name("{page0}-{total}", pdf, 3, 9, "jpg", 200), "2-9")
        # 模板里带非法字符也要被清理
        self.assertEqual(render_name("a/b:{page}", pdf, 1, 1, "png", 200), "a_b_1")

    def test_iter_pdf_inputs(self) -> None:
        make_pdf(self.tmp / "one.pdf")
        make_pdf(self.tmp / "sub" / "two.pdf")
        (self.tmp / "note.txt").write_text("x", encoding="utf-8")
        (self.tmp / "pic.png").write_bytes(b"\x89PNG")
        pdfs, ignored = iter_pdf_inputs([self.tmp])
        self.assertEqual([p.name for p in pdfs], ["one.pdf", "two.pdf"])
        self.assertEqual(len(ignored), 2)
        pdfs_flat, _ = iter_pdf_inputs([self.tmp], recursive=False)
        self.assertEqual([p.name for p in pdfs_flat], ["one.pdf"])
        # 重复路径去重（大小写不敏感）
        pdfs_dup, _ = iter_pdf_inputs([self.tmp / "one.pdf", str(self.tmp / "one.pdf")])
        self.assertEqual(len(pdfs_dup), 1)


# --------------------------------------------------------------------------- #
# 转换引擎
# --------------------------------------------------------------------------- #
class TestConvert(TempDirCase):
    def test_defaults_are_best_quality(self) -> None:
        """默认设置必须是画质最好的组合：无损 PNG + 300 DPI + 质量 100。"""
        self.assertEqual(DEFAULT_FORMAT, "png")
        self.assertEqual(DEFAULT_DPI, 300)
        self.assertEqual(DEFAULT_QUALITY, 100)
        options = ConvertOptions()
        self.assertEqual(options.image_format, "png")
        self.assertEqual(options.dpi, 300)
        self.assertEqual(options.quality, 100)
        # 无损 PNG 真实渲染出来也是 300 DPI 的尺寸
        pdf = make_pdf(self.tmp / "默认.pdf")
        report = convert_files([pdf], ConvertOptions(output_dir=self.out))
        with self.read_image(report.outputs[0]) as image:
            self.assertEqual(image.format, "PNG")  # PNG 即无损
            self.assertAlmostEqual(image.width, 2479, delta=6)   # A4 @300dpi
            self.assertAlmostEqual(image.height, 3508, delta=6)
            self.assertAlmostEqual(image.info.get("dpi")[0], 300, delta=1)

    def test_webp_quality_100_is_lossless(self) -> None:
        """质量 100 时 WebP 用无损编码：解码后应与无损 PNG 逐像素一致。"""
        from PIL import ImageChops

        pdf = make_pdf(self.tmp / "a.pdf")
        png = convert_files(
            [pdf], ConvertOptions(output_dir=self.out / "png", image_format="png", dpi=100)
        )
        webp = convert_files(
            [pdf], ConvertOptions(output_dir=self.out / "webp", image_format="webp",
                                  quality=100, dpi=100)
        )
        self.assertTrue(webp.outputs[0].name.endswith(".webp"))
        with self.read_image(png.outputs[0]) as a, self.read_image(webp.outputs[0]) as b:
            self.assertEqual(b.format, "WEBP")
            self.assertIsNone(
                ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox(),
                "质量 100 的 WebP 应该是无损的",
            )

    def test_higher_quality_jpg_is_closer_to_original(self) -> None:
        """质量 100 的 JPG 应比质量 60 更接近无损原图（用像素误差衡量）。"""
        from PIL import ImageChops, ImageStat

        pdf = make_pdf(self.tmp / "a.pdf")
        png = convert_files(
            [pdf], ConvertOptions(output_dir=self.out / "ref", image_format="png", dpi=100)
        )
        low = convert_files(
            [pdf], ConvertOptions(output_dir=self.out / "q60", image_format="jpg", quality=60, dpi=100)
        )
        high = convert_files(
            [pdf], ConvertOptions(output_dir=self.out / "q100", image_format="jpg", quality=100, dpi=100)
        )

        def mean_error(path) -> float:
            with self.read_image(png.outputs[0]) as ref, self.read_image(path) as image:
                diff = ImageChops.difference(ref.convert("RGB"), image.convert("RGB"))
                return sum(ImageStat.Stat(diff).mean) / 3

        self.assertLess(mean_error(high.outputs[0]), mean_error(low.outputs[0]))

    def test_batch_two_pdfs(self) -> None:
        a = make_pdf(self.tmp / "发票A.pdf", pages=2)
        b = make_pdf(self.tmp / "发票B.pdf", pages=3)
        report = convert_files([a, b], ConvertOptions(output_dir=self.out, dpi=100))
        self.assertEqual([f.status for f in report.files], ["ok", "ok"])
        self.assertEqual(report.done_pages, 5)
        self.assertEqual(len(report.outputs), 5)
        names = sorted(p.name for p in report.outputs)
        self.assertEqual(
            names,
            ["发票A_p001.png", "发票A_p002.png", "发票B_p001.png", "发票B_p002.png", "发票B_p003.png"],
        )
        for path in report.outputs:
            with self.read_image(path) as image:
                self.assertEqual(image.mode, "RGB")
                # A4 @100dpi ≈ 827×1169
                self.assertAlmostEqual(image.width, 826, delta=4)
                self.assertAlmostEqual(image.height, 1169, delta=4)
                self.assertAlmostEqual(image.info.get("dpi")[0], 100, delta=1)

    def test_dpi_changes_size(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf")
        low = convert_files([pdf], ConvertOptions(output_dir=self.out / "low", dpi=72))
        high = convert_files([pdf], ConvertOptions(output_dir=self.out / "high", dpi=300))
        with self.read_image(low.outputs[0]) as a, self.read_image(high.outputs[0]) as b:
            self.assertAlmostEqual(a.width, 595, delta=3)
            self.assertAlmostEqual(b.width, 2479, delta=6)
            self.assertGreater(b.width, a.width * 4 - 5)

    def test_jpg_format_and_quality(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf")
        report = convert_files(
            [pdf], ConvertOptions(output_dir=self.out, image_format="jpg", quality=60, dpi=120)
        )
        self.assertTrue(report.outputs[0].name.endswith(".jpg"))
        self.assertEqual(report.outputs[0].suffix, ".jpg")
        with self.read_image(report.outputs[0]) as image:
            self.assertEqual(image.format, "JPEG")

    def test_grayscale(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf")
        report = convert_files(
            [pdf], ConvertOptions(output_dir=self.out, grayscale=True, image_format="jpg")
        )
        with self.read_image(report.outputs[0]) as image:
            self.assertEqual(image.mode, "L")

    def test_page_range_subset(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf", pages=5)
        report = convert_files(
            [pdf], ConvertOptions(output_dir=self.out, page_range="2-3,5", dpi=72)
        )
        self.assertEqual(sorted(p.name for p in report.outputs),
                         ["a_p002.png", "a_p003.png", "a_p005.png"])

    def test_template_and_extension_field(self) -> None:
        pdf = make_pdf(self.tmp / "账单.pdf", pages=2)
        report = convert_files(
            [pdf],
            ConvertOptions(output_dir=self.out, name_template="{name}-{page}of{total}", dpi=72),
        )
        self.assertEqual(sorted(p.name for p in report.outputs), ["账单-1of2.png", "账单-2of2.png"])

    def test_no_overwrite_creates_unique_names(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf")
        options = ConvertOptions(output_dir=self.out, dpi=72)
        first = convert_files([pdf], options)
        second = convert_files([pdf], ConvertOptions(output_dir=self.out, dpi=72))
        self.assertEqual(first.outputs[0].name, "a_p001.png")
        self.assertEqual(second.outputs[0].name, "a_p001 (2).png")
        self.assertTrue(second.outputs[0].exists())

    def test_overwrite_replaces(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf")
        convert_files([pdf], ConvertOptions(output_dir=self.out, dpi=72))
        target = self.out / "a_p001.png"
        before = target.stat().st_mtime_ns
        report = convert_files([pdf], ConvertOptions(output_dir=self.out, dpi=72, overwrite=True))
        self.assertEqual(report.outputs[0], target)
        self.assertNotEqual(target.stat().st_mtime_ns, before)

    def test_subfolder_per_pdf(self) -> None:
        a = make_pdf(self.tmp / "甲.pdf", pages=2)
        b = make_pdf(self.tmp / "乙.pdf", pages=1)
        report = convert_files(
            [a, b], ConvertOptions(output_dir=self.out, subfolder_per_pdf=True, dpi=72)
        )
        relative = sorted(str(p.relative_to(self.out)).replace("\\", "/") for p in report.outputs)
        self.assertEqual(relative, ["乙/乙_p001.png", "甲/甲_p001.png", "甲/甲_p002.png"])

    def test_default_output_dir_is_cwd(self) -> None:
        pdf = make_pdf(self.tmp / "默认目录.pdf")
        workdir = self.tmp / "cwd"
        workdir.mkdir()
        with contextlib.chdir(workdir):
            report = convert_files([pdf], ConvertOptions(dpi=72))
            self.assertEqual(report.output_dir, workdir)
            self.assertTrue((workdir / "默认目录_p001.png").exists())

    def test_default_output_dir_falls_back_when_cwd_unwritable(self) -> None:
        """macOS 双击 .app 时当前目录是 /（不可写），应该退回桌面/主目录。"""
        from unittest import mock

        from invoice2img import core

        with mock.patch.object(core.os, "access", return_value=False):
            fallback = core.default_output_dir()
        self.assertIn(fallback, {Path.home(), Path.home() / "Desktop"})
        self.assertNotEqual(fallback, Path.cwd())
        self.assertTrue(fallback.is_absolute())
        self.assertTrue(fallback.is_dir())
        # 显式指定了目录时，不受“当前目录不可写”的影响
        with mock.patch.object(core.os, "access", return_value=False):
            self.assertEqual(ConvertOptions(output_dir=self.out).resolved_output_dir(), self.out)

    def test_non_pdf_inputs_are_ignored(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf")
        stray = self.tmp / "readme.txt"
        stray.write_text("hello", encoding="utf-8")
        report = convert_files([pdf, stray], ConvertOptions(output_dir=self.out, dpi=72))
        self.assertEqual(len(report.ignored), 1)
        self.assertEqual(report.ignored[0], stray)
        self.assertEqual(len(report.succeeded), 1)
        self.assertIn("忽略 1", report.summary())

    def test_encrypted_pdf_without_password_fails(self) -> None:
        pdf = make_pdf(self.tmp / "locked.pdf", encrypted=True)
        report = convert_files([pdf], ConvertOptions(output_dir=self.out))
        outcome = report.files[0]
        self.assertEqual(outcome.status, "failed")
        self.assertIn("密码", outcome.error or "")

    def test_encrypted_pdf_with_password_succeeds(self) -> None:
        pdf = make_pdf(self.tmp / "locked.pdf", pages=2, encrypted=True)
        report = convert_files(
            [pdf], ConvertOptions(output_dir=self.out, password="1234", dpi=72)
        )
        self.assertEqual(report.files[0].status, "ok")
        self.assertEqual(len(report.outputs), 2)

    def test_corrupt_pdf_reports_error(self) -> None:
        broken = self.tmp / "broken.pdf"
        broken.write_bytes(b"%PDF-1.7 not really a pdf")
        report = convert_files([broken], ConvertOptions(output_dir=self.out))
        self.assertEqual(report.files[0].status, "failed")
        self.assertTrue(report.files[0].error)

    def test_cancel_stops_early(self) -> None:
        pdf = make_pdf(self.tmp / "big.pdf", pages=60)
        cancel = threading.Event()
        seen: list[int] = []

        def on_progress(event) -> None:
            if event.event == "page":
                seen.append(event.done)
                if event.done >= 3:
                    cancel.set()

        report = convert_files(
            [pdf],
            ConvertOptions(output_dir=self.out, dpi=200, workers=2),
            on_progress=on_progress,
            cancel_event=cancel,
        )
        self.assertTrue(report.cancelled)
        self.assertLess(len(report.outputs), 60)
        self.assertGreaterEqual(len(report.outputs), 1)

    def test_directory_input_recursive(self) -> None:
        make_pdf(self.tmp / "src" / "a.pdf")
        make_pdf(self.tmp / "src" / "nested" / "b.pdf")
        report = convert_files([self.tmp / "src"], ConvertOptions(output_dir=self.out, dpi=72))
        self.assertEqual(len(report.files), 2)
        self.assertEqual(len(report.outputs), 2)

    def test_oversized_page_is_rejected_without_allocating(self) -> None:
        huge = make_pdf(self.tmp / "huge.pdf", size=(5000.0, 5000.0))
        report = convert_files([huge], ConvertOptions(output_dir=self.out, dpi=1200))
        self.assertEqual(report.files[0].status, "failed")
        self.assertIn("DPI", report.files[0].error or "")

    def test_invalid_dpi_and_format(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf")
        with self.assertRaises(ConversionError):
            convert_files([pdf], ConvertOptions(output_dir=self.out, dpi=99999))
        with self.assertRaises(ConversionError):
            convert_files([pdf], ConvertOptions(output_dir=self.out, image_format="bmp"))

    def test_describe_pdf(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf", pages=4)
        info = describe_pdf(pdf)
        self.assertTrue(info.ok)
        self.assertEqual(info.pages, 4)
        self.assertFalse(info.encrypted)
        self.assertTrue(describe_pdf(self.tmp / "missing.pdf").error)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
class TestCli(TempDirCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = cli.main(argv)
        return code, buffer.getvalue()

    def test_cli_batch_conversion(self) -> None:
        make_pdf(self.tmp / "src" / "a.pdf", pages=2)
        make_pdf(self.tmp / "src" / "b.pdf")
        code, output = self.run_cli([str(self.tmp / "src"), "-o", str(self.out), "-d", "72"])
        self.assertEqual(code, 0, output)
        self.assertEqual(len(list(self.out.glob("*.png"))), 3)
        self.assertIn("导出目录", output)

    def test_cli_default_output_is_cwd(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf")
        workdir = self.tmp / "cli-cwd"
        workdir.mkdir()
        with contextlib.chdir(workdir):
            code, output = self.run_cli([str(pdf), "-d", "72"])
        self.assertEqual(code, 0, output)
        self.assertTrue((workdir / "a_p001.png").exists())

    def test_cli_pages_and_format(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf", pages=4)
        code, _ = self.run_cli(
            [str(pdf), "-o", str(self.out), "-f", "jpg", "-p", "2-3", "--gray", "-d", "72"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in self.out.glob("*.jpg")), ["a_p002.jpg", "a_p003.jpg"])
        with self.read_image(self.out / "a_p002.jpg") as image:
            self.assertEqual(image.mode, "L")

    def test_cli_glob_and_template(self) -> None:
        make_pdf(self.tmp / "g" / "发票1.pdf")
        make_pdf(self.tmp / "g" / "发票2.pdf")
        pattern = str(self.tmp / "g" / "*.pdf")
        code, output = self.run_cli(
            [pattern, "-o", str(self.out), "-t", "IMG-{page:02d}-{name}", "-d", "72"]
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(
            sorted(p.name for p in self.out.glob("*.png")),
            ["IMG-01-发票1.png", "IMG-01-发票2.png"],
        )

    def test_cli_missing_file_returns_error_code(self) -> None:
        code, output = self.run_cli([str(self.tmp / "nope.pdf"), "-o", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("没有找到任何 PDF", output)

    def test_cli_password(self) -> None:
        pdf = make_pdf(self.tmp / "locked.pdf", pages=2, encrypted=True)
        code, output = self.run_cli(
            [str(pdf), "-o", str(self.out), "--password", "1234", "-d", "72"]
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(len(list(self.out.glob("*.png"))), 2)

    def test_cli_help_and_version(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        with self.assertRaises(SystemExit) as ctx2:
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["--version"])
        self.assertEqual(ctx2.exception.code, 0)

    def test_cli_no_inputs_prints_help(self) -> None:
        code, output = self.run_cli([])
        self.assertEqual(code, 1)
        self.assertIn("usage", output.lower())

    def test_cli_selftest(self) -> None:
        report = self.tmp / "selftest.txt"
        code, output = self.run_cli(["--selftest", "--selftest-report", str(report)])
        self.assertEqual(code, 0, output)
        text = report.read_text(encoding="utf-8")
        self.assertIn("全部通过", text)
        self.assertIn("PDF 转图片", text)
        self.assertIn("PyMuPDF", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
