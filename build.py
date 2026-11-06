# -*- coding: utf-8 -*-
"""build.py —— 一键构建 ScreenOffWeb.exe 与 MSI 安装包。

用法：
    python build.py            # 构建 exe + msi
    python build.py exe        # 只构建 exe
    python build.py msi        # 只构建 msi（需先有 exe）

依赖：
    pip install pyinstaller pystray pillow
    WiX v3.14 解压在 tools/wix314/（build.py 会检查）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
TOOLS_WIX = os.path.join(ROOT, "tools", "wix314")
VERSION = "1.1.0"
APP_EXE = "ScreenOffWeb.exe"
MSI_NAME = f"ScreenOffWeb-{VERSION}.msi"

VERSION_INFO = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=(1, 1, 0, 0),
    prodvers=(1, 1, 0, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '080404b0',
        [StringStruct('CompanyName', 'atqiyu'),
         StringStruct('FileDescription', 'ScreenOff-Web - LAN monitor switch'),
         StringStruct('FileVersion', '{VERSION}'),
         StringStruct('InternalName', 'ScreenOffWeb'),
         StringStruct('LegalCopyright', 'Copyright (c) 2026 atqiyu. MIT License.'),
         StringStruct('OriginalFilename', '{APP_EXE}'),
         StringStruct('ProductName', 'ScreenOff-Web'),
         StringStruct('ProductVersion', '{VERSION}')])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def log(msg: str) -> None:
    print(f"\n=== {msg} ===")


def build_exe() -> str:
    import icon

    os.makedirs(DIST, exist_ok=True)
    ico = os.path.join(ROOT, "icon.ico")
    icon.save_ico(ico)
    vfile = os.path.join(BUILD, "version_info.txt")
    os.makedirs(BUILD, exist_ok=True)
    with open(vfile, "w", encoding="utf-8") as f:
        f.write(VERSION_INFO)

    log("PyInstaller 构建 ScreenOffWeb.exe")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--noconsole",
        "--name", "ScreenOffWeb",
        "--icon", ico,
        "--version-file", vfile,
        "--add-data", "index.html;.",
        "--hidden-import", "pystray._win32",
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "app.py",
    ]
    subprocess.run(cmd, cwd=ROOT, check=True)
    exe = os.path.join(DIST, APP_EXE)
    if not os.path.exists(exe):
        raise SystemExit(f"构建失败：{exe} 不存在")
    shutil.copy2(ico, os.path.join(DIST, "icon.ico"))
    size = os.path.getsize(exe) / 1024 / 1024
    print(f"[OK] {exe}  ({size:.1f} MB)")
    return exe


def _wix(name: str) -> str:
    path = os.path.join(TOOLS_WIX, name)
    if not os.path.exists(path):
        raise SystemExit(
            f"未找到 WiX：{path}\n"
            "请下载 wix314-binaries.zip 解压到 tools/wix314/")
    return path


def build_msi() -> str:
    exe = os.path.join(DIST, APP_EXE)
    if not os.path.exists(exe):
        raise SystemExit("请先构建 exe：python build.py exe")
    if not os.path.exists(os.path.join(DIST, "icon.ico")):
        import icon
        icon.save_ico(os.path.join(DIST, "icon.ico"))

    candle, light = _wix("candle.exe"), _wix("light.exe")
    obj = os.path.join(BUILD, "Product.wixobj")
    msi = os.path.join(DIST, MSI_NAME)
    wxs = os.path.join(ROOT, "installer", "Product.wxs")

    log("编译 WiX 源文件")
    subprocess.run([
        candle, "-nologo", "-arch", "x64",
        "-ext", "WixUIExtension",
        "-ext", "WixUtilExtension",
        "-ext", "WixFirewallExtension",
        f"-dVersion={VERSION}", f"-dSourceDir={DIST}",
        f"-dInstallerDir={os.path.join(ROOT, 'installer')}",
        "-out", obj, wxs,
    ], cwd=ROOT, check=True)

    log("链接 MSI 安装包")
    subprocess.run([
        light, "-nologo", "-sval",
        "-ext", "WixUIExtension",
        "-ext", "WixUtilExtension",
        "-ext", "WixFirewallExtension",
        "-cultures:zh-CN;en-US",
        "-out", msi, obj,
    ], cwd=ROOT, check=True)
    print(f"[OK] {msi}  ({os.path.getsize(msi)/1024/1024:.2f} MB)")
    return msi


def main() -> int:
    targets = sys.argv[1:] or ["exe", "msi"]
    if "exe" in targets:
        build_exe()
    if "msi" in targets:
        build_msi()
    return 0


if __name__ == "__main__":
    sys.exit(main())