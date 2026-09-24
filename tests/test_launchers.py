from __future__ import annotations

import base64
import codecs
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1]
LAUNCHERS = ("install.ps1", "change-check.ps1")


class Launchers(unittest.TestCase):
    def run_powershell(self, shell, script, cwd):
        executable = shutil.which(shell)
        if executable is None:
            self.skipTest(f"{shell} is not installed")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["CHANGE_CHECK_HOME"] = str(Path(cwd) / "state")
        env["CHANGE_CHECK_USER_HOME"] = str(Path(cwd) / "user")
        return subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd=cwd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )

    def check_file_parser(self, shell):
        with tempfile.TemporaryDirectory(prefix="change-check-launchers-") as directory:
            root = Path(directory) / "中文 空格"
            root.mkdir()
            for name in LAUNCHERS:
                with self.subTest(launcher=name):
                    target = root / name
                    shutil.copyfile(TOOL / name, target)
                    quoted = str(target).replace("'", "''")
                    script = f"""
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{quoted}', [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) {{
    $parseErrors | ForEach-Object {{ Write-Output $_.ErrorId }}
    exit 1
}}
if ('{name}' -eq 'install.ps1') {{
    $strings = $ast.FindAll({{
        param($node)
        $node -is [System.Management.Automation.Language.StringConstantExpressionAst]
    }}, $true) | ForEach-Object {{ $_.Value }}
    if ($strings -notcontains '无法建立独立 Python 环境') {{ exit 2 }}
    if ($strings -notcontains '依赖安装失败，尚未修改钩子') {{ exit 3 }}
}}
"""
                    result = self.run_powershell(shell, script, directory)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def check_help(self, shell):
        with tempfile.TemporaryDirectory(prefix="change-check-help-") as directory:
            target = str(TOOL / "change-check.ps1").replace("'", "''")
            script = f"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
& '{target}' --help
exit $LASTEXITCODE
"""
            result = self.run_powershell(shell, script, directory)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("--help", result.stdout)
            self.assertIn("变更脚本检查", result.stdout)
            self.assertFalse((Path(directory) / "state").exists())
            self.assertFalse((Path(directory) / "user").exists())

    def test_scripts_declare_utf8_for_windows_powershell(self):
        for name in LAUNCHERS:
            with self.subTest(launcher=name):
                content = (TOOL / name).read_bytes()
                self.assertTrue(content.startswith(codecs.BOM_UTF8))
                content.decode("utf-8-sig")

    def check_default_directory(self, shell):
        if shutil.which(shell) is None:
            self.skipTest(f"{shell} is not installed")
        with tempfile.TemporaryDirectory(prefix="change-check-directory-") as directory:
            root = Path(directory)
            tool = root / "工具 目录"
            project = root / "项目 '目录"
            (tool / "src").mkdir(parents=True)
            project.mkdir()
            # Isolated interpreter and pip stub: no downloads or real installation.
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(tool / ".venv")],
                           check=True, capture_output=True, timeout=30)
            (project / "pip.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            (tool / "src" / "main.py").write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
            for name in LAUNCHERS:
                target = tool / name
                shutil.copyfile(TOOL / name, target)
                command = [] if name == "install.ps1" else ["install"]
                for explicit in ([], ["--root", str(root / "指定 目录")]):
                    with self.subTest(launcher=name, explicit=bool(explicit)):
                        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
                        arguments = " ".join(map(quote, command + explicit))
                        script = (f"Set-Location -LiteralPath {quote(project)}\n"
                                  f"& {quote(target)} {arguments}\nexit $LASTEXITCODE\n")
                        result = self.run_powershell(shell, script, directory)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(json.loads(result.stdout),
                                         ["--default-root", str(project), "install", *explicit])
            self.assertFalse((root / "state").exists())
            self.assertFalse((root / "user").exists())

    def test_windows_powershell_file_parser(self):
        self.check_file_parser("powershell.exe")

    def test_powershell_core_file_parser(self):
        self.check_file_parser("pwsh")

    def test_windows_powershell_help(self):
        self.check_help("powershell.exe")

    def test_powershell_core_help(self):
        self.check_help("pwsh")

    def test_windows_powershell_current_directory(self):
        self.check_default_directory("powershell.exe")

    def test_powershell_core_current_directory(self):
        self.check_default_directory("pwsh")


if __name__ == "__main__":
    unittest.main()
