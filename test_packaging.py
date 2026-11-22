#!/usr/bin/env python3
"""Windows 安装包配置回归测试。

保证两个 Windows 安装包（NSIS setup.exe / WiX MSI）都默认安装到
%LOCALAPPDATA%\\Programs\\ZedG、以当前用户权限安装、产品名为 ZedG，
并且 zedg-test 只作为旧版兼容常量存在，不会重新变成默认目录或产品名。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NSI = ROOT / "packaging" / "windows" / "zedg-setup.nsi"
WXS = ROOT / ".github" / "installer" / "zedg.wxs"
BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "02-build.yml"

WIX_NS = {"w": "http://schemas.microsoft.com/wix/2006/wi"}
EXPECTED_DIR = r"$LOCALAPPDATA\Programs\ZedG"


def nsi_text() -> str:
    return NSI.read_text(encoding="utf-8-sig")


def nsi_define(name: str) -> str | None:
    m = re.search(rf'^!define\s+{re.escape(name)}\s+"([^"]*)"', nsi_text(), re.MULTILINE)
    return m.group(1) if m else None


def test_nsis_default_dir_is_localappdata() -> None:
    m = re.search(r'^InstallDir\s+"([^"]+)"', nsi_text(), re.MULTILINE)
    assert m, "zedg-setup.nsi 缺少 InstallDir"
    assert m.group(1) == EXPECTED_DIR, f"默认安装目录是 {m.group(1)!r}"


def test_nsis_default_dir_has_no_hardcoded_drive() -> None:
    for line in nsi_text().splitlines():
        if re.match(r'^InstallDir\s+"', line):
            assert not re.search(r"[A-Za-z]:\\", line), line


def test_nsis_installs_per_user_without_admin() -> None:
    text = nsi_text()
    assert re.search(r"^RequestExecutionLevel\s+user\b", text, re.MULTILINE), \
        "安装包必须声明 RequestExecutionLevel user（默认安装不弹 UAC）"
    assert "InstallDirRegKey HKCU" in text, \
        "已安装机器必须沿用 HKCU 记录的目录，避免升级时装到新目录"


def test_nsis_product_name_is_zedg() -> None:
    assert nsi_define("APP_NAME") == "ZedG", nsi_define("APP_NAME")
    text = nsi_text()
    assert 'Name "${APP_NAME}"' in text
    assert '"DisplayName" "${APP_NAME}' in text
    assert '"UninstallString" \'"$INSTDIR\\Uninstall.exe"\'' in text


def test_nsis_zedg_test_only_as_legacy_constant() -> None:
    """zedg-test 只能出现在 LEGACY_* 常量或注释里，不能成为产品名/默认目录。"""
    for line in nsi_text().splitlines():
        if "zedg-test" in line.lower():
            stripped = line.strip()
            assert stripped.startswith("!define LEGACY_") or stripped.startswith(";"), \
                f"zedg-test 出现在非兼容位置: {line!r}"


def test_workflow_does_not_override_install_dir() -> None:
    text = BUILD_WORKFLOW.read_text(encoding="utf-8")
    assert "zedg-test" not in text, "构建 workflow 不应出现 zedg-test"
    assert not re.search(r"(?i)install[_-]?dir\s*=", text), \
        "构建 workflow 不应覆盖安装包默认目录"


def test_msi_is_per_user_under_localappdata() -> None:
    root = ET.parse(WXS).getroot()
    package = root.find("w:Product/w:Package", WIX_NS)
    assert package is not None, "zedg.wxs 缺少 Package"
    assert package.get("InstallScope") == "perUser", package.get("InstallScope")

    product = root.find("w:Product", WIX_NS)
    assert product is not None and product.get("Name") == "ZedG", "MSI 产品名必须是 ZedG"

    local = root.find(".//w:Directory[@Id='LocalAppDataFolder']", WIX_NS)
    assert local is not None, "MSI 必须安装在 LocalAppDataFolder 下"
    programs = local.find("w:Directory[@Id='ProgramsFolder']", WIX_NS)
    assert programs is not None, r"MSI 目录层级应为 LocalAppDataFolder\Programs"
    install = programs.find("w:Directory[@Id='INSTALLFOLDER']", WIX_NS)
    assert install is not None and install.get("Name") == "ZedG", "安装目录名必须是 ZedG"
    assert "ProgramFiles64Folder" not in WXS.read_text(encoding="utf-8"), \
        "MSI 不应再使用 Program Files（那需要管理员权限）"


def test_msi_shortcuts_are_named_zedg() -> None:
    root = ET.parse(WXS).getroot()
    names = [
        s.get("Name")
        for s in root.iter("{http://schemas.microsoft.com/wix/2006/wi}Shortcut")
    ]
    assert names and all(n == "ZedG" for n in names), names


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
