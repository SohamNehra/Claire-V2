"""Actual Windows shell execution and timeout cleanup, without a model."""
import os
import sys
import tempfile
import unittest

from claire_core.guard import Sandbox
from claire_core.settings import Settings


@unittest.skipUnless(sys.platform == "win32", "Windows process behavior")
class WindowsShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.shell = Sandbox(Settings(sandbox="off"))

    def test_approved_command_can_use_powershell_and_paths_with_spaces(self):
        result = self.shell.run(
            "Set-Content -LiteralPath 'hello world.txt' -Value 'Claire ready'; "
            "Get-Content -LiteralPath 'hello world.txt'",
            cwd=self.temp.name, sandboxed=False)
        self.assertEqual(result.returncode, 0, result.output)
        self.assertEqual(result.output.strip(), "Claire ready")
        self.assertTrue(os.path.isfile(os.path.join(self.temp.name, "hello world.txt")))

    def test_timeout_ends_child_process_too(self):
        script = os.path.join(self.temp.name, "wait.py")
        pidfile = os.path.join(self.temp.name, "child.pid")
        with open(script, "w", encoding="utf-8") as out:
            out.write("import pathlib, subprocess, sys, time\n"
                      "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                      "pathlib.Path('child.pid').write_text(str(child.pid))\n"
                      "time.sleep(30)\n")
        result = self.shell.run([sys.executable, script], cwd=self.temp.name,
                                timeout=2, sandboxed=False)
        self.assertTrue(result.timed_out)
        with open(pidfile, encoding="utf-8") as saved:
            pid = int(saved.read())
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        # A live process has a kernel handle; a terminated child has none.
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            try:
                code = wintypes.DWORD()
                self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                self.assertNotEqual(code.value, 259, "child survived the command timeout")
            finally:
                kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
