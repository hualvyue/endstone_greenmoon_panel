# endstone entry point.
#
# ★ 打包时 entry point 名与发行名必须**配套**，否则 Endstone 拒收：
#     [project].name = "endstone-greenmoon-panel"
#     [project.entry-points."endstone"]
#     greenmoon-panel = "admin_web.plugin:GreenMoonPlugin"
#   即：发行名 == "endstone-" + entry point 名（严格相等，下划线视同连字符）。
#   名字不合规时插件会走 whl 兜底加载，导致 data_folder 拿不到 ——
#   数据目录/日志被迫落到 plugins/.local/... 下并随时丢失。
#   运行时自查见 admin_web/_packaging_check.py。
from .core import GreenMoonPlugin

__all__ = ["GreenMoonPlugin"]
