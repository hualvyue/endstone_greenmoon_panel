"""打包规范自查 —— 发行名（distribution name）是否合规。

★ 为什么要单独查这个
-------------------
Endstone 对 entry-point 型插件有**硬性命名要求**
（``endstone/plugin/plugin_loader.py``）::

    if not ep.dist.name.replace("_", "-").startswith("endstone-"):
        server.logger.error(
            f"Error occurred when trying to load plugin from entry point '{ep.name}': Invalid name.")
        server.logger.error(
            f"The name of distribution ({ep.dist.name}) does not start with 'endstone-' or 'endstone_'.")
        return None

    dist_name = "endstone-" + ep.name.replace("_", "-")
    if ep.dist.name.replace("_", "-") != dist_name:
        ...  # 也必须精确相等
        return None

也就是说：**发行名必须严格等于 ``endstone-`` + entry point 名**。
例如 entry point 叫 ``greenmoon-panel``，发行名就必须是
``endstone-greenmoon-panel``。

一旦不合规，Endstone 会**拒收** entry point 通路，插件改走 whl 兜底加载。
兜底通路下 C++ 侧不会注入 ``PluginDescription``，于是 ``plugin.data_folder``
**取属性即抛异常** —— 表现为：

* ``data_folder`` 拿不到 → 数据目录解析失败；
* 日志被迫回退到 ``plugins/.local/...``（Endstone 装 wheel 的派生目录），
  该目录随时会被重建清理，于是日志反复丢失、报 ``write failed``。

这个坑很难从现象反推，所以在启动时主动查一次，并在控制台/面板上**直说**。
"""

from __future__ import annotations

from typing import Any, List, Optional

__all__ = ["check_dist_name", "EXPECTED_DIST_NAME", "EXPECTED_ENTRY_NAME"]

#: 本插件**应该**用的发行名（== "endstone-" + entry point 名）
EXPECTED_DIST_NAME = "endstone-greenmoon-panel"

#: 本插件的 entry point 名
EXPECTED_ENTRY_NAME = "greenmoon-panel"

#: 依次尝试探测的发行名：优先期望值，其余是历史/误写的名字，用于定位症状
_CANDIDATE_NAMES = (
    EXPECTED_DIST_NAME,
    "greenmoon-panel",
    "greenmoon_panel",
    "endstone_greenmoon_panel",
)


def _normalize(name: str) -> str:
    """归一化发行名：下划线统一成连字符，便于比较。

    PEP 503 规定发行名比较时 ``-`` 与 ``_`` 等价，Endstone 也照此做了
    ``.replace("_", "-")``，这里保持一致。
    """
    try:
        return str(name).replace("_", "-").strip().lower()
    except Exception:
        return ""


def _find_distribution() -> Optional[Any]:
    """找到本插件对应的 distribution 对象；找不到返回 None。"""
    try:
        import importlib.metadata as md
    except Exception:
        return None
    for name in _CANDIDATE_NAMES:
        try:
            return md.distribution(name)
        except Exception:
            continue
    return None


def check_dist_name(logger: Any = None) -> Optional[str]:
    """自查发行名。**不合规**时返回错误说明串，合规或查不到时返回 None。

    参数
    ----
    logger
        可选。给了就顺手把结果打进控制台（不合规用 ``error``，合规不打扰）。

    返回
    ----
    ``None``
        发行名合规，或环境里根本查不到 distribution（例如以源码目录直接
        运行、未打包安装）。后者**不算错误** —— 不打包的场景本来就没有
        发行名，没必要报错吓人。
    ``str``
        不合规的具体原因，可直接展示给用户。
    """
    try:
        dist = _find_distribution()
        if dist is None:
            return None

        try:
            actual = dist.metadata["Name"]
        except Exception:
            actual = getattr(dist, "name", None) or getattr(dist, "_name", None)
        if not actual:
            return None

        norm = _normalize(actual)
        expected_norm = _normalize(EXPECTED_DIST_NAME)

        if norm == expected_norm:
            # 合规：不打扰用户
            return None

        if not norm.startswith("endstone-"):
            msg = (
                f"发行名 '{actual}' 不合规：必须以 'endstone-' 开头。"
                f"Endstone 会拒收 entry point，插件改走 whl 兜底加载，"
                f"导致 data_folder 不可用（数据目录/日志会落到 plugins/.local/... 下）。"
                f"请把 pyproject 的 [project].name 改成 '{EXPECTED_DIST_NAME}'。"
            )
        else:
            msg = (
                f"发行名 '{actual}' 与 entry point 名不匹配："
                f"必须严格等于 'endstone-' + entry point 名，即 '{expected_norm}'。"
                f"请检查 pyproject 的 [project].name 与 "
                f"[project.entry-points.\"endstone\"] 的键是否配套。"
            )

        if logger is not None:
            try:
                logger.error(f"[打包] {msg}")
            except Exception:
                pass
        return msg
    except Exception:
        # 自查失败绝不能影响插件启动
        return None
