#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GreenMoon 面板 · 离线依赖下载打包工具

把子插件与 QQ 机器人所需的第三方库，一次性下载成「离线可用」的 wheel 集合，
并打包成可直接放进 GreenMoon 处理流程的归档。

产出物（默认写到 dist/）
------------------------
1. ``greenmoon-libs-all.zip``
   适合本工具产出的通用包：内部为 ``manifest.json`` + 各平台 .whl 目录。
   直接改名成 ``.gmlib`` 交给面板的「子插件 → 导入包」即可自动入库。
2. ``<平台>-<python>/`` 分平台目录（``--split``）
   每个目录内只放该平台可用的 wheel，直接整个拷进插件的 ``libs/`` 即可。

用法示例
--------
    # 默认：当前平台 + 目标 Python，下载面板运行必需的两个库
    python3 fetch_deps.py

    # 全平台 · 全版本（体积很大，见 --help 的说明）
    python3 fetch_deps.py --all-platforms --all-python

    # 只给 Linux/Windows + Python 3.11/3.12，并额外带上一个子插件的依赖
    python3 fetch_deps.py -p linux,win -y 3.11,3.12 -r requests websockets "Pillow>=10"

    # 已有依赖清单文件时
    python3 fetch_deps.py -r requirements.txt

依赖
----
仅需 Python 3.8+ 与可用的 pip（工具本身只用标准库）。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

# --------------------------------------------------------------------------
# 目标矩阵定义
# --------------------------------------------------------------------------

#: 平台别名 -> (pip --platform 取值, 人类可读说明)
#: Endstone 目前提供 Linux / Windows 的服务端，macOS 仅作预留。
PLATFORM_MAP: Dict[str, Tuple[str, str]] = {
    "linux-x64":   ("manylinux2014_x86_64",  "Linux x86_64（主流云主机 / 宝塔 / Docker）"),
    "linux-arm64": ("manylinux2014_aarch64", "Linux ARM64（甲骨文 ARM、树莓派 64 位）"),
    "win-x64":     ("win_amd64",             "Windows x86_64（面板最常用的开服环境）"),
    "win-arm64":   ("win_arm64",             "Windows ARM64"),
    "macos-x64":   ("macosx_11_0_x86_64",    "macOS Intel"),
    "macos-arm64": ("macosx_11_0_arm64",     "macOS Apple Silicon"),
}

#: 平台别名 -> sys.platform 前缀，用于自动推断当前平台
_OS_PREFIX = {
    "linux": "linux",
    "win": "win32",
    "macos": "darwin",
}

#: Python 版本 -> 解释器自身标签
PYTHON_VERSIONS = ["3.8", "3.9", "3.10", "3.11", "3.12", "3.13"]

#: 面板运行必需（QQ 机器人网关）的库
REQUIRED_PACKAGES = ["requests", "websockets"]

#: 各平台下的「无 wheel」提示：pip 找不到二进制包时会尝试源码构建，离线场景不可用
FREE_THREADED = ["3.13t"]


def _os_of(alias: str) -> str:
    return alias.split("-")[0]


def current_platform_alias() -> str:
    """推断当前运行平台对应的别名。"""
    osname = "linux"
    if sys.platform.startswith("win"):
        osname = "win"
    elif sys.platform == "darwin":
        osname = "macos"

    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "x64"
    return f"{osname}-{arch}"


def detect_ssl_context_ok() -> bool:
    """粗略检查 pip 是否可用。"""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True, text=True, timeout=30,
        )
        return out.returncode == 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------

def run_pip_download(
    packages: Sequence[str],
    dest: Path,
    *,
    platform_tag: str | None = None,
    python_version: str | None = None,
    abi: str | None = None,
    only_binary: bool = True,
    extra_args: Sequence[str] = (),
) -> Tuple[bool, str]:
    """调用 pip download 拉取 wheel。

    返回 ``(成功与否, 输出文本)``。失败时不抛异常，便于批量任务继续跑。
    """
    dest.mkdir(parents=True, exist_ok=True)
    cmd: List[str] = [
        sys.executable, "-m", "pip", "download",
        *packages,
        "-d", str(dest),
        "--no-cache-dir",
        "--disable-pip-version-check",
    ]
    if only_binary:
        cmd.append("--only-binary=:all:")
    if platform_tag:
        cmd += ["--platform", platform_tag]
    if python_version:
        cmd += ["--python-version", python_version]
    if abi:
        cmd += ["--implementation", "cp", "--abi", abi]
    cmd += list(extra_args)

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except subprocess.TimeoutExpired:
        return False, "下载超时（30 分钟）"
    except Exception as e:
        return False, f"调用 pip 失败: {e}"

    text = (out.stdout or "") + (out.stderr or "")
    return out.returncode == 0, text


def collect_wheels(folder: Path) -> List[Path]:
    return sorted(p for p in folder.rglob("*.whl") if p.is_file())


def collect_sdists(folder: Path) -> List[Path]:
    out = []
    for pat in ("*.tar.gz", "*.zip", "*.tar.bz2"):
        out += [p for p in folder.rglob(pat) if p.is_file()]
    return sorted(out)


# --------------------------------------------------------------------------
# 打包
# --------------------------------------------------------------------------

def _split_single_vs_multi(
    wheels_by_platform: Dict[str, List[Path]],
) -> Tuple[Dict[str, List[Path]], Dict[str, List[Path]], List[str], List[str]]:
    """按「包名在归档里是否只有一份 wheel」把 wheel 分成两组。

    面板的 ``.gmlib`` 导入器是**按包名匹配**的：它在归档里从头扫描，遇到第一个
    同名 ``.whl`` 就采用，之后再遇到同名的会按「已安装」跳过。

    单平台包每个包名天然只有一份，直接导入没问题；
    但全平台矩阵里同一个包会有 ``cp311`` / ``cp313`` / ``win_amd64`` 等多个变体，
    第一个命中的未必是当前平台可用的那份 —— 这就是导入会串平台的原因。

    返回 ``(safe, risky, safe_pkgs, risky_pkgs)``：

    * ``safe``  —— 该包名全归档只此一份，可写进 ``liblist`` 让面板自动匹配
    * ``risky`` —— 同名多份，只放进 ``t-<平台>-py<版本>/`` 供人工按目标拷贝
    """
    counts: Dict[str, int] = {}
    for wl in wheels_by_platform.values():
        for w in wl:
            counts[_pkg_name(w)] = counts.get(_pkg_name(w), 0) + 1

    safe: Dict[str, List[Path]] = {}
    risky: Dict[str, List[Path]] = {}
    for alias, wl in wheels_by_platform.items():
        for w in wl:
            target = risky if counts[_pkg_name(w)] > 1 else safe
            target.setdefault(alias, []).append(w)

    safe_pkgs = sorted({_pkg_name(w) for wl in safe.values() for w in wl})
    risky_pkgs = sorted({_pkg_name(w) for wl in risky.values() for w in wl})
    return safe, risky, safe_pkgs, risky_pkgs


def _is_single_target(n_platforms: int, n_pythons: int) -> bool:
    """判断本次产出是否只对应「单一平台 + 单一 Python」。

    只有这种情况，改名 ``.gmlib`` 直接导入才是绝对安全的。
    """
    return n_platforms == 1 and n_pythons == 1


def build_gmlib(
    out_zip: Path,
    wheels_by_platform: Dict[str, List[Path]],
    packages: Sequence[str],
    *,
    name: str = "greenmoon-libs",
    safe_import: bool = True,
) -> Tuple[Path, int, List[str]]:
    """把各平台 wheel 打成可被面板直接导入的 .gmlib（本质是 zip + manifest）。

    目录结构::

        manifest.json
        <平台>/<wheel>              # 可安全进 liblist 的
        t-<平台>-py<版本>/<wheel>    # 同名多份的变体，仅供手工拷贝

    ``liblist`` 只列「全归档唯一」的包名。同名多份的包名既不进 ``liblist``
    （否则导入会串平台），仍在归档里以 ``t-`` 前缀保留，供人工取用。

    返回 ``(zip 路径, wheel 总数, 需要人工拷贝的包名列表)``。
    """
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    safe, risky, safe_pkgs, risky_pkgs = _split_single_vs_multi(wheels_by_platform)

    flat = sorted({w.name for wl in wheels_by_platform.values() for w in wl})

    if safe_import:
        # 单目标：所有 wheel 天然唯一，全部交给面板自动匹配
        pkgs = sorted({_pkg_name(w) for wl in wheels_by_platform.values() for w in wl})
        unsafe_pkgs: List[str] = []
    else:
        # 多目标：只把唯一的包名交给 liblist，其余要求人工按目标拷贝
        pkgs = safe_pkgs
        unsafe_pkgs = risky_pkgs

    manifest: Dict[str, Any] = {
        "name": name,
        "python_version": _python_span(),
        "platform": "any",
        "liblist": pkgs,
        # 以下为扩展字段，面板导入时会忽略
        "x-greenmoon": {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "generator": "fetch_deps.py",
            "source_packages": list(packages),
            "wheels": flat,
            "safe_to_import": bool(safe_import and not risky_pkgs),
            "platforms": {k: [w.name for w in v] for k, v in wheels_by_platform.items()},
        },
    }
    if unsafe_pkgs:
        manifest["x-greenmoon"]["needs_manual_copy"] = unsafe_pkgs

    count = 0
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        # 安全的：按平台平铺，文件名全局唯一
        for alias, wheels in safe.items():
            for w in wheels:
                zf.write(w, f"{alias}/{w.name}")
                count += 1
        # 同名多份的：另存 t- 目录，且**不再在上面的 <平台>/ 里重复一份**，
        # 避免同一文件在归档里存两遍、体积白翻倍。
        for alias, wheels in risky.items():
            for w in wheels:
                zf.write(w, f"t-{alias}/{w.name}")
                count += 1
    return out_zip, count, unsafe_pkgs


def _pkg_name(whl: Path) -> str:
    """从 wheel 文件名取归一化包名（wheel 规范里 name 段不含连字符）。"""
    stem = whl.name[:-4] if whl.name.lower().endswith(".whl") else whl.name
    return stem.split("-")[0].lower().replace("_", "-")


def _python_span() -> str:
    if len(PYTHON_VERSIONS) == 1:
        return PYTHON_VERSIONS[0]
    return "any"


def parse_requirements(path: Path) -> List[str]:
    """读取 requirements.txt / 自定义清单，忽略注释与空行。"""
    pkgs: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        pkgs.append(line)
    return pkgs


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def resolve_targets(args) -> Tuple[List[str], List[str]]:
    """把命令行参数解析成 (平台别名列表, python 版本列表)。"""
    if args.all_platforms:
        plats = list(PLATFORM_MAP.keys())
    elif args.platform:
        plats = [p.strip() for p in args.platform.split(",") if p.strip()]
    else:
        plats = [current_platform_alias()]

    bad = [p for p in plats if p not in PLATFORM_MAP]
    if bad:
        raise SystemExit(
            f"未知平台: {', '.join(bad)}\n可选: {', '.join(PLATFORM_MAP.keys())}"
        )

    if args.all_python:
        pys = list(PYTHON_VERSIONS)
    elif args.python:
        pys = [p.strip() for p in args.python.split(",") if p.strip()]
    else:
        major, minor = sys.version_info[:2]
        pys = [f"{major}.{minor}"]

    for p in pys:
        if p not in PYTHON_VERSIONS:
            raise SystemExit(
                f"不支持的 Python 版本: {p}\n可选: {', '.join(PYTHON_VERSIONS)}"
            )
    return plats, pys


def main() -> int:
    ap = argparse.ArgumentParser(
        description="GreenMoon 面板离线依赖下载打包工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法示例")[-1],
    )
    ap.add_argument("-r", "--requirements", nargs="*", default=None,
                    help="要下载的包（可多个），或一个 requirements.txt 路径；"
                         "默认下载面板必需的 requests 与 websockets")
    ap.add_argument("-p", "--platform", default=None,
                    help="平台，逗号分隔。可选：" + ", ".join(PLATFORM_MAP.keys()))
    ap.add_argument("-y", "--python", default=None,
                    help="Python 版本，逗号分隔。可选：" + ", ".join(PYTHON_VERSIONS))
    ap.add_argument("--all-platforms", action="store_true",
                    help="下载全部平台（6 个组合，体积很大）")
    ap.add_argument("--all-python", action="store_true",
                    help="下载全部 Python 版本（3.8~3.13）")
    ap.add_argument("-o", "--out", default="dist",
                    help="输出目录（默认 dist）")
    ap.add_argument("--name", default="greenmoon-libs",
                    help="产出的 .gmlib 包名")
    ap.add_argument("--split", action="store_true",
                    help="额外按「平台-Python版本」拆分输出目录，便于直接拷进 libs/")
    ap.add_argument("--keep-sdist", action="store_true",
                    help="保留源码包（.tar.gz）；默认丢弃，因为离线环境无法编译")
    ap.add_argument("--allow-sdist", action="store_true",
                    help="允许 pip 回退到源码包（默认强制只取 wheel）")
    ap.add_argument("--no-zip", action="store_true",
                    help="只下载，不打包成 .gmlib")
    ap.add_argument("--extra-pip-arg", action="append", default=[],
                    help="追加给 pip 的额外参数，可重复，如 "
                         "--extra-pip-arg=--index-url --extra-pip-arg=https://pypi.tuna.tsinghua.edu.cn/simple")
    args = ap.parse_args()

    if not detect_ssl_context_ok():
        print("✗ 找不到可用的 pip，请确认 python -m pip 可执行", file=sys.stderr)
        return 2

    # 解析依赖列表
    if args.requirements:
        pkgs: List[str] = []
        for item in args.requirements:
            p = Path(item)
            if p.is_file():
                pkgs += parse_requirements(p)
                print(f"· 从 {p} 读取到 {len(pkgs)} 项依赖")
            else:
                pkgs.append(item)
        packages = pkgs or list(REQUIRED_PACKAGES)
    else:
        packages = list(REQUIRED_PACKAGES)

    plats, pys = resolve_targets(args)
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    combos = [(pl, py) for pl in plats for py in pys]

    print("=" * 68)
    print("GreenMoon 离线依赖下载")
    print("=" * 68)
    print(f"依赖     : {', '.join(packages)}")
    print(f"平台     : {', '.join(plats)}")
    print(f"Python   : {', '.join(pys)}")
    print(f"组合数   : {len(combos)}")
    print(f"输出目录 : {out_root}")
    print("=" * 68)

    wheels_by_platform: Dict[str, List[Path]] = {}
    failed: List[str] = []
    seen_names: set = set()

    for idx, (alias, pyver) in enumerate(combos, 1):
        plat_tag, desc = PLATFORM_MAP[alias]
        label = f"{alias}-py{pyver}"
        print(f"\n[{idx}/{len(combos)}] {label}  —  {desc}")

        tmp = Path(tempfile.mkdtemp(prefix="gmwhl_"))
        try:
            ok, text = run_pip_download(
                packages, tmp,
                platform_tag=plat_tag,
                python_version=pyver,
                only_binary=not args.allow_sdist,
                extra_args=tuple(args.extra_pip_arg),
            )
            wheels = collect_wheels(tmp)
            if not ok and not wheels:
                reason = _first_error_line(text)
                print(f"    ✗ 失败: {reason}")
                failed.append(f"{label}: {reason}")
                continue

            # 同名 wheel 只保留一份，避免 .gmlib 体积翻倍
            fresh = []
            for w in wheels:
                if w.name in seen_names:
                    continue
                seen_names.add(w.name)
                fresh.append(w)

            bucket = wheels_by_platform.setdefault(alias, [])
            for w in fresh:
                dest = out_root / alias / w.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(w, dest)
                bucket.append(dest)

            print(f"    ✓ {len(fresh)} 个 wheel"
                  + (f"（{len(wheels) - len(fresh)} 个重复已跳过）" if len(wheels) > len(fresh) else ""))

            if args.keep_sdist:
                for s in collect_sdists(tmp):
                    dest = out_root / alias / "sdist" / s.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(s, dest)
                    print(f"    · 保留源码包 {s.name}")

            if args.split:
                tgt = out_root / "by-target" / label
                tgt.mkdir(parents=True, exist_ok=True)
                for w in fresh:
                    shutil.copy2(w, tgt / w.name)

        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- 汇总 ----------------
    total = sum(len(v) for v in wheels_by_platform.values())
    print("\n" + "=" * 68)
    print(f"下载完成：{total} 个 wheel，覆盖 {len(wheels_by_platform)} 个平台")
    if failed:
        print(f"\n以下 {len(failed)} 个组合未能完成：")
        for f in failed:
            print(f"  · {f}")
        print("\n提示：多数情况是该 Python 版本 / 平台组合下，包没有对应的预编译 wheel。")
        print("     这类组合对 Endstone 面板无意义，可以直接忽略。")

    if total and not args.no_zip:
        safe = _is_single_target(len(plats), len(pys))
        zip_path = out_root / f"{args.name}-all.zip"
        path, count, manual = build_gmlib(
            zip_path, wheels_by_platform, packages,
            name=args.name, safe_import=safe,
        )
        size = path.stat().st_size / 1024 / 1024
        print(f"\n已打包：{path}")
        print(f"        含 {count} 个 wheel，{size:.1f} MB")

        if safe:
            print(f"\n使用方式（任选其一）：")
            print(f"  1) 改名导入：把 {path.name} 重命名为 .gmlib 后，")
            print(f"     在面板「🔌 子插件 → 导入包」上传，会自动解包并登记依赖。")
            print(f"  2) 直接拷贝：把 {out_root}/<平台>/ 下的 .whl 拷进")
            print(f"     插件数据目录的 libs/ ，重启面板即可生效。")
        else:
            print(f"\n⚠ 本次为跨平台 / 跨版本矩阵，一个包名对应多份 wheel。")
            print(f"  面板的 .gmlib 导入器是按「包名」匹配的，取到哪个变体不确定，")
            print(f"  所以**不要**把整个 zip 改名 .gmlib 直接导入。")
            if manual:
                print(f"\n  以下 {len(manual)} 个包名存在多平台变体，只能在归档的")
                print(f"  t-<平台>-py<版本>/ 目录里取，请按部署目标手工拷贝：")
                print(f"      {', '.join(manual)}")
            print(f"\n  正确用法：挑对应的目标目录，把其中 .whl 拷进")
            print(f"  插件数据目录的 libs/ ，然后重启面板：")
            for alias, pyver in combos[:3]:
                print(f"      {out_root}/by-target/{alias}-py{pyver}/")
            if len(combos) > 3:
                print(f"      …… 共 {len(combos)} 个目标目录，见 {out_root}/by-target/")
            if not args.split:
                print(f"\n  （加 --split 可自动生成上述分目标目录）")
            print(f"\n  或：对每个目标各跑一次本工具（不带 --all-platforms），")
            print(f"      得到的就是可直接改名 .gmlib 导入的单平台包。")

    if args.split:
        print(f"\n分平台目录：{out_root}/by-target/<平台-py版本>/")
    print()
    return 0 if not failed else 1


def _first_error_line(text: str) -> str:
    """从 pip 输出里抓一行最能说明问题的信息。"""
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if any(k in low for k in ("error", "no matching distribution",
                                  "could not find", "not find a version",
                                  "failed", "is not a supported wheel")):
            return s[:200]
    tail = [l.strip() for l in (text or "").splitlines() if l.strip()]
    return tail[-1][:200] if tail else "未知错误"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
