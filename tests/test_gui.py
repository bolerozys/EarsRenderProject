# -*- coding: utf-8 -*-
"""图形界面测试：窗口构建、拖拽解析、批量转换全流程。

在没有图形环境（例如纯 SSH 会话）的机器上会自动跳过，不会让测试失败。
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice2img import gui  # noqa: E402

try:  # 作为包导入时（python -m unittest discover -s tests -t .）
    from .test_core import make_pdf  # noqa: E402
except ImportError:  # 作为顶层模块导入时（python -m unittest discover -s tests）
    from test_core import make_pdf  # noqa: E402


class FakeMessageBox:
    """挡住真实弹窗，避免自动化测试卡住。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _record(self, kind: str, title: str, message: str = "", **_kwargs):
        self.calls.append((kind, str(message)))
        return True

    showinfo = lambda self, title, message="", **kw: self._record("info", title, message, **kw)  # noqa: E731
    showwarning = lambda self, title, message="", **kw: self._record("warning", title, message, **kw)  # noqa: E731
    showerror = lambda self, title, message="", **kw: self._record("error", title, message, **kw)  # noqa: E731
    askyesno = lambda self, title, message="", **kw: self._record("askyesno", title, message, **kw)  # noqa: E731
    askokcancel = lambda self, title, message="", **kw: self._record("askokcancel", title, message, **kw)  # noqa: E731

    def messages(self) -> str:
        return "\n".join(text for _kind, text in self.calls)


class FakeSimpleDialog:
    """自动“取消”密码输入框，避免测试阻塞。"""

    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer

    def askstring(self, *_args, **_kwargs):
        return self.answer


class TestGui(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pdf2img-gui-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self._real_messagebox = gui.messagebox
        self._real_simpledialog = gui.simpledialog
        self.fake_box = FakeMessageBox()
        gui.messagebox = self.fake_box  # type: ignore[assignment]
        gui.simpledialog = FakeSimpleDialog()  # type: ignore[assignment]
        self.addCleanup(setattr, gui, "messagebox", self._real_messagebox)
        self.addCleanup(setattr, gui, "simpledialog", self._real_simpledialog)

        try:
            self.root = gui.create_root()
        except Exception as exc:  # pragma: no cover - 无显示环境
            self.skipTest(f"无法创建 Tk 窗口：{exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.app = gui.App(self.root)

    def _destroy_root(self) -> None:
        # 走应用自己的关闭流程，取消挂起的 after 回调（等价于点窗口关闭按钮）
        app = getattr(self, "app", None)
        if app is not None:
            try:
                app.destroy()
                return
            except Exception:  # pragma: no cover
                pass
        try:
            self.root.destroy()
        except Exception:  # pragma: no cover
            pass

    # ---------------------------------------------------------------- 工具
    def pump(self, predicate, timeout: float = 60.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.root.update()
            if predicate():
                return True
            time.sleep(0.02)
        self.root.update()
        return predicate()

    def wait_scan(self, timeout: float = 20.0) -> None:
        self.pump(lambda: all(e.status != "读取中…" for e in self.app.entries), timeout)

    def drop(self, *paths: Path) -> None:
        """模拟一次拖拽落下事件（走真实的 tkdnd 数据格式：路径用花括号包裹）。"""
        data = " ".join("{%s}" % p for p in paths)
        self.app._on_drop(SimpleNamespace(data=data))

    # ---------------------------------------------------------------- 用例
    def test_window_has_expected_widgets(self) -> None:
        self.assertTrue(self.app.tree.winfo_exists())
        self.assertEqual(str(self.app.btn_start["text"]), "开始转换")
        self.assertEqual(str(self.app.btn_cancel["state"]), "disabled")
        self.assertEqual(self.app.var_target.get(), "cwd")  # 默认导出到当前目录
        self.assertIn("当前目录", self.app.dir_entry.master.winfo_children()[0]["text"])

    def test_gui_defaults_are_best_quality(self) -> None:
        """界面默认值 = 最高画质：无损 PNG + 300 DPI + 质量 100。"""
        self.assertTrue(self.app.var_format.get().startswith("png"))
        self.assertEqual(self.app.var_dpi.get(), "300")
        self.assertEqual(self.app.var_quality.get(), "100")
        options = self.app.collect_options()
        self.assertEqual(options.image_format, "png")
        self.assertEqual(options.dpi, "300")
        self.assertEqual(options.quality, 100)
        self.assertIsNone(options.output_dir)  # 未设置 => 当前目录

    def test_dnd_available_or_graceful(self) -> None:
        # 本机已装 tkinterdnd2，应能成功注册拖拽目标；未装时界面给出安装提示
        if gui.DND_AVAILABLE:
            self.app.drop_frame.drop_target_register(gui.DND_FILES)
        else:  # pragma: no cover
            self.assertIn("tkinterdnd2", self.app.lbl_status.cget("text") + self.app.drop_label.cget("text"))

    def test_drop_multiple_pdfs_with_spaces_in_name(self) -> None:
        a = make_pdf(self.tmp / "增值税发票 001.pdf", pages=2)
        b = make_pdf(self.tmp / "增值税发票 002.pdf", pages=1)
        self.drop(a, b)
        self.wait_scan()
        names = [e.path.name for e in self.app.entries]
        self.assertEqual(names, ["增值税发票 001.pdf", "增值税发票 002.pdf"])
        self.assertEqual([e.pages for e in self.app.entries], [2, 1])
        self.assertIn("已添加 2 个 PDF", self.app.lbl_status.cget("text"))
        rows = self.app.tree.item(self.app.entries[0].iid, "values")
        self.assertEqual(str(rows[2]), "2")
        self.assertEqual(str(rows[3]), "待转换")

    def test_drop_folder_expands_pdfs_and_ignores_others(self) -> None:
        make_pdf(self.tmp / "src" / "a.pdf")
        make_pdf(self.tmp / "src" / "nested" / "b.pdf")
        (self.tmp / "src" / "note.txt").write_text("x", encoding="utf-8")
        self.drop(self.tmp / "src")
        self.wait_scan()
        self.assertEqual(sorted(e.path.name for e in self.app.entries), ["a.pdf", "b.pdf"])
        self.assertIn("忽略 1", self.app.lbl_status.cget("text"))

    def test_drop_is_deduplicated(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf")
        self.drop(pdf)
        self.drop(pdf)
        self.wait_scan()
        self.assertEqual(len(self.app.entries), 1)
        self.assertIn("已添加 0 个 PDF", self.app.lbl_status.cget("text"))

    def test_remove_and_clear(self) -> None:
        a = make_pdf(self.tmp / "a.pdf")
        b = make_pdf(self.tmp / "b.pdf")
        self.drop(a, b)
        self.wait_scan()
        self.app.tree.selection_set(self.app.entries[0].iid)
        self.app.remove_selected()
        self.assertEqual([e.path.name for e in self.app.entries], ["b.pdf"])
        self.app.clear_all()
        self.assertEqual(self.app.entries, [])
        self.assertEqual(self.app.tree.get_children(), ())

    def test_encrypted_pdf_shows_reason(self) -> None:
        pdf = make_pdf(self.tmp / "locked.pdf", encrypted=True)
        self.drop(pdf)
        self.wait_scan()
        # 自动化环境里 simpledialog 未 stub，直接断言列表状态即可
        entry = self.app.entries[0]
        self.assertIsNotNone(entry.error)
        self.assertIn("加密", entry.error or "")

    def test_full_conversion_through_gui(self) -> None:
        a = make_pdf(self.tmp / "发票甲.pdf", pages=2)
        b = make_pdf(self.tmp / "发票乙.pdf", pages=1)
        out = self.tmp / "导出"
        self.drop(a, b)
        self.wait_scan()

        self.app.var_target.set("custom")
        self.app.var_custom_dir.set(str(out))
        self.app.var_format.set("png — PNG（无损，推荐用于发票）")
        self.app.var_dpi.set("120")
        self.app.var_open_dir.set(False)

        self.app.start_conversion()
        self.assertTrue(self.pump(lambda: not self.app._running, timeout=90))

        files = sorted(p.name for p in out.glob("*.png"))
        self.assertEqual(
            files, sorted(["发票甲_p001.png", "发票甲_p002.png", "发票乙_p001.png"])
        )
        self.assertEqual(len(files), 3)
        for entry in self.app.entries:
            self.assertTrue(entry.status.startswith("✓"), entry.status)
            self.assertEqual(self.app.tree.item(entry.iid, "tags"), ("ok",))
        self.assertIn("完成：2 个文件 / 3 张图片", self.fake_box.messages())
        self.assertEqual(self.app.progress["value"], 100)

    def test_gui_respects_source_directory_option(self) -> None:
        folder = self.tmp / "源目录"
        pdf = make_pdf(folder / "发票.pdf")
        self.drop(pdf)
        self.wait_scan()
        self.app.var_target.set("source")
        self.app.start_conversion()
        self.assertTrue(self.pump(lambda: not self.app._running, timeout=60))
        self.assertTrue((folder / "发票_p001.png").exists())

    def test_gui_default_output_is_current_directory(self) -> None:
        import contextlib

        workdir = self.tmp / "cwd"
        workdir.mkdir()
        pdf = make_pdf(self.tmp / "发票.pdf")
        self.drop(pdf)
        self.wait_scan()
        self.assertEqual(self.app.var_target.get(), "cwd")
        self.app.var_dpi.set("100")
        with contextlib.chdir(workdir):
            self.app.start_conversion()
            self.assertTrue(self.pump(lambda: not self.app._running, timeout=60))
        self.assertTrue((workdir / "发票_p001.png").exists())

    def test_gui_cancel(self) -> None:
        pdf = make_pdf(self.tmp / "大文件.pdf", pages=40)
        out = self.tmp / "out"
        self.drop(pdf)
        self.wait_scan()
        self.app.var_target.set("custom")
        self.app.var_custom_dir.set(str(out))
        self.app.var_dpi.set("300")
        self.app.start_conversion()
        self.root.update()
        self.app.cancel_conversion()
        self.assertTrue(self.pump(lambda: not self.app._running, timeout=90))
        self.assertLess(len(list(out.glob("*.png"))), 40)

    def test_gui_numeric_validation(self) -> None:
        pdf = make_pdf(self.tmp / "a.pdf")
        self.drop(pdf)
        self.wait_scan()
        self.app.var_dpi.set("abc")
        self.app.start_conversion()
        self.assertTrue(self.pump(lambda: not self.app._running, timeout=30))
        self.assertIn("DPI", self.fake_box.messages())
        self.fake_box.calls.clear()
        self.app.var_target.set("custom")
        self.app.var_custom_dir.set("")
        self.app.start_conversion()
        self.assertFalse(self.app._running)
        self.assertIn("导出目录", self.fake_box.messages())

    def test_gui_no_files_shows_hint(self) -> None:
        self.app.start_conversion()
        self.assertIn("请先拖入", self.fake_box.messages())


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
