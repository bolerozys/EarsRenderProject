# -*- coding: utf-8 -*-
"""打包相关文件的“体检”：确保在 Windows / macOS 上都能正确构建。

这些用例可以在任意平台上跑，能提前发现几类很容易踩的坑：
* ``.command`` / ``.sh`` 用了 CRLF 换行（macOS 上双击直接报错）
* ``.bat`` 用了 LF 换行（cmd 解析 goto/标签时会出问题）
* shell 脚本语法错误（有 bash 时用 ``bash -n`` 真正检查一遍）
* 脚本里引用的文件不存在（比如改了文件名忘了改脚本）
* PyInstaller spec 语法错误、Pillow 生成的 .ico 尺寸不全
* GitHub Actions 工作流 YAML 不合法
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


class TestLineEndings(unittest.TestCase):
    def test_unix_scripts_use_lf(self) -> None:
        files = list(ROOT.glob("*.command")) + list(ROOT.glob("packaging/*.sh"))
        self.assertTrue(files, "没有找到 .command / .sh 脚本")
        for path in files:
            data = read_bytes(path)
            self.assertNotIn(
                b"\r\n", data,
                f"{path.name} 含 CRLF 换行，macOS 上会执行失败",
            )
            self.assertTrue(data.startswith(b"#!"), f"{path.name} 缺少 shebang")

    def test_batch_files_use_crlf(self) -> None:
        files = list(ROOT.glob("*.bat")) + list(ROOT.glob("packaging/*.bat"))
        self.assertTrue(files, "没有找到 .bat 脚本")
        for path in files:
            text = read_bytes(path).decode("utf-8")
            self.assertIn("\r\n", text, f"{path.name} 应用 CRLF 换行")
            # 除了行尾的 \r，不应有其它裸 \r
            for index, line in enumerate(text.split("\n")):
                self.assertFalse(
                    line.endswith("\r") is False and "\r" in line,
                    f"{path.name} 第 {index + 1} 行有异常回车符",
                )


class TestRequirementsFiles(unittest.TestCase):
    """requirements 必须是合法需求清单（曾经因为写了三引号说明导致 pip 直接报错）。"""

    def _lines(self, name: str) -> list[str]:
        path = ROOT / name
        self.assertTrue(path.exists(), f"缺少 {name}")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn('"""', text, f"{name} 里不能用三引号字符串，pip 会当成需求名")
        return [
            line.split("#", 1)[0].strip()
            for line in text.splitlines()
            if line.split("#", 1)[0].strip()
        ]

    def test_requirements_parse_like_pip(self) -> None:
        for name in ("requirements.txt", "requirements-build.txt"):
            lines = self._lines(name)
            self.assertTrue(lines, f"{name} 里没有任何依赖")
            for line in lines:
                self.assertRegex(
                    line,
                    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?"
                    r"\s*([<>=!~;].*)?$",
                    f"{name} 里的 {line!r} 不像是合法的 pip 需求",
                )

    def test_requirements_cover_runtime_deps(self) -> None:
        joined = " ".join(self._lines("requirements.txt")).lower()
        for package in ("pymupdf", "pillow", "tkinterdnd2"):
            self.assertIn(package, joined, f"requirements.txt 缺少 {package}")


class TestTestDiscovery(unittest.TestCase):
    """CI 上真实踩过的坑：``tests`` 不是包时，unittest 在 loader.py:308 直接报
    ``ImportError: Start directory is not importable``。

    这里用子进程把两种官方调用方式都跑一遍，确保以后不会再退化。
    """

    def test_tests_directory_is_a_package(self) -> None:
        self.assertTrue(
            (ROOT / "tests" / "__init__.py").exists(),
            "tests/ 需要 __init__.py，否则 discover 在部分 Python 版本上会失败",
        )

    def test_discovery_commands_succeed(self) -> None:
        commands = [
            # 我平时用的方式
            [sys.executable, "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_core.py"],
            # CI 里的方式（显式指定仓库根为 top-level，最容易触发该问题）
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".",
             "-p", "test_core.py"],
        ]
        for command in commands:
            result = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=600,
            )
            self.assertEqual(
                result.returncode, 0,
                "命令失败：" + " ".join(command)
                + "\n--- stdout ---\n" + result.stdout[-1200:]
                + "\n--- stderr ---\n" + result.stderr[-1200:],
            )

    def test_workflow_pins_top_level_dir(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build-desktop.yml")
        if not workflow.exists():
            self.skipTest("没有 CI 工作流")
        text = workflow.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "unittest" in line and "discover" in line:
                self.assertIn("-t .", line, f"CI 的测试命令应显式指定 -t .：{line.strip()}")

    def test_workflow_passes_python_to_build_script(self) -> None:
        """构建脚本读的是 $PYTHON；CI 必须导出同名变量，否则会用系统 python3。"""
        workflow = (ROOT / ".github" / "workflows" / "build-desktop.yml")
        if not workflow.exists():
            self.skipTest("没有 CI 工作流")
        text = workflow.read_text(encoding="utf-8")
        if "build_macos.sh" in text:
            lines = text.splitlines()
            exported = any("GITHUB_ENV" in line and "PYTHON=" in line for line in lines) or any(
                line.strip().startswith("PYTHON:") for line in lines
            )
            self.assertTrue(
                exported,
                "CI 调用 build_macos.sh 时必须导出 PYTHON（指向带 Tk 的解释器）",
            )
        # run_macos.command / build_macos.sh 都要用 $PYTHON
        for script in ("packaging/build_macos.sh", "run_macos.command"):
            content = (ROOT / script).read_text(encoding="utf-8")
            self.assertIn('PYTHON="${PYTHON:-python3}"', content, f"{script} 应支持用 $PYTHON 覆盖解释器")


    def test_app_names_are_consistent(self) -> None:
        """spec / 构建脚本 / CI 里的 App 名字必须一致，否则压缩和上传步骤会找不到产物。"""
        name = "PDF发票转图片"
        spec = (ROOT / "packaging" / "pdf2image.spec").read_text(encoding="utf-8")
        script = (ROOT / "packaging" / "build_macos.sh").read_text(encoding="utf-8")
        self.assertIn(f'GUI_NAME = "{name}"', spec)
        self.assertIn(f'APP_NAME="{name}"', script)
        workflow = ROOT / ".github" / "workflows" / "build-desktop.yml"
        if workflow.exists():
            text = workflow.read_text(encoding="utf-8")
            self.assertIn(f"APP_NAME: {name}", text)
            self.assertIn('"$APP_NAME.app"', text)


class TestShellSyntax(unittest.TestCase):
    def setUp(self) -> None:
        self.bash = shutil.which("bash") or shutil.which("bash.exe")
        if not self.bash:
            for candidate in (
                r"C:\Program Files\Git\bin\bash.exe",
                r"C:\Program Files\Git\usr\bin\bash.exe",
                "/bin/bash",
            ):
                if Path(candidate).exists():
                    self.bash = candidate
                    break
        if not self.bash:
            self.skipTest("本机没有 bash，跳过 shell 语法检查")

    def test_syntax(self) -> None:
        files = sorted(list(ROOT.glob("*.command")) + list(ROOT.glob("packaging/*.sh")))
        for path in files:
            result = subprocess.run(
                [self.bash, "-n", str(path)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            self.assertEqual(
                result.returncode, 0,
                f"{path.name} 语法错误：\n{result.stderr}",
            )

    def test_no_windows_line_ending_dependency(self) -> None:
        """脚本里不要出现 CR 或依赖 Windows 路径分隔符。"""
        for path in sorted(list(ROOT.glob("*.command")) + list(ROOT.glob("packaging/*.sh"))):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("\r", text, f"{path.name} 含回车符")
            self.assertNotIn("C:\\", text, f"{path.name} 里出现了 Windows 路径")


class TestReferencedPathsExist(unittest.TestCase):
    """脚本里写到的项目文件必须真实存在，避免改名后脚本失效。"""

    def test_scripts_reference_existing_files(self) -> None:
        expected = [
            "pdf2image_app.py",
            "requirements.txt",
            "requirements-build.txt",
            "packaging/pdf2image.spec",
            "tools/make_icon.py",
            "invoice2img/core.py",
            "invoice2img/gui.py",
            "invoice2img/cli.py",
        ]
        for relative in expected:
            self.assertTrue((ROOT / relative).exists(), f"缺少 {relative}")

    def test_scripts_mention_required_paths(self) -> None:
        build_mac = (ROOT / "packaging" / "build_macos.sh").read_text(encoding="utf-8")
        for token in ("pdf2image.spec", "requirements.txt", "tools/make_icon.py", "iconutil",
                      "codesign", "xattr", "com.apple.quarantine"):
            self.assertIn(token, build_mac, f"build_macos.sh 里缺少 {token}")

    def test_wrapper_command_invokes_build_script(self) -> None:
        wrapper = (ROOT / "打包Mac客户端.command").read_text(encoding="utf-8")
        self.assertIn("packaging/build_macos.sh", wrapper)

    def test_run_macos_uses_venv(self) -> None:
        text = (ROOT / "run_macos.command").read_text(encoding="utf-8")
        self.assertIn("venv", text)
        self.assertIn(".venv-mac", text)

    def test_run_windows_uses_entry(self) -> None:
        text = (ROOT / "run_windows.bat").read_text(encoding="utf-8")
        self.assertIn("pdf2image_app.py", text)
        self.assertIn("requirements.txt", text)


class TestSpecFile(unittest.TestCase):
    def test_spec_compiles(self) -> None:
        source = (ROOT / "packaging" / "pdf2image.spec").read_text(encoding="utf-8")
        compile(source, "pdf2image.spec", "exec")  # 语法错误会在这里抛出

    def test_spec_has_macos_specifics(self) -> None:
        source = (ROOT / "packaging" / "pdf2image.spec").read_text(encoding="utf-8")
        for token in ("icon.icns", "BUNDLE", "PDF2IMG_TARGET_ARCH", "NSHighResolutionCapable",
                      "tkinterdnd2"):
            self.assertIn(token, source, f"spec 里缺少 {token}")


class TestIcons(unittest.TestCase):
    def test_windows_ico_has_common_sizes(self) -> None:
        from PIL import Image

        ico = ROOT / "assets" / "icon.ico"
        self.assertTrue(ico.exists(), "缺少 assets/icon.ico")
        with Image.open(ico) as image:
            sizes = sorted(image.info.get("sizes", set()))
        self.assertIn((256, 256), sizes)
        self.assertIn((32, 32), sizes)
        self.assertIn((16, 16), sizes)

    def test_png_icon_is_square_and_large(self) -> None:
        from PIL import Image

        for name in ("icon.png", "icon-256.png"):
            path = ROOT / "assets" / name
            self.assertTrue(path.exists(), f"缺少 assets/{name}")
            with Image.open(path) as image:
                self.assertEqual(image.width, image.height)
                self.assertGreaterEqual(image.width, 256)


class TestGithubWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = ROOT / ".github" / "workflows" / "build-desktop.yml"
        if not self.workflow.exists():
            self.skipTest("没有 CI 工作流")
        try:
            import yaml  # type: ignore
        except ImportError:
            self.skipTest("没有安装 PyYAML，跳过 YAML 校验")
        self.yaml = yaml

    def test_yaml_parses_and_has_two_platforms(self) -> None:
        data = self.yaml.safe_load(self.workflow.read_text(encoding="utf-8"))
        # YAML 1.1 会把裸 on: 解析成 True，两种键名都兼容
        triggers = data.get("on", data.get(True))
        self.assertIn("workflow_dispatch", triggers)
        jobs = data["jobs"]
        self.assertIn("macos", jobs)
        self.assertIn("windows", jobs)
        arches = [item["arch"] for item in jobs["macos"]["strategy"]["matrix"]["include"]]
        self.assertEqual(arches, ["arm64", "x86_64"])
        steps = jobs["macos"]["steps"]
        self.assertTrue(any("build_macos.sh" in str(step.get("run", "")) for step in steps))

    def test_workflow_uploads_artifacts(self) -> None:
        text = self.workflow.read_text(encoding="utf-8")
        self.assertIn("upload-artifact", text)
        self.assertIn("ditto -c -k", text)  # .app 必须先压缩


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
