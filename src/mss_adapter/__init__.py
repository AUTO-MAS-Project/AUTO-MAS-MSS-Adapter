#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#   SPDX-License-Identifier: AGPL-3.0-or-later

"""MSS（MaaStellaSora / 星塔旅人）专项插件。

MSS 托管跑本包自己的两层流程（``manager.MSSManager`` → ``account_run.MSSAccountRun``，配置只经
``config_view`` 读写）；项目更新、运行池、agent 环境、worker 运行、判定、记账、通知正文等调 MFW 三块积木插件
（``maafw_project_update`` / ``maafw_agent_env`` / ``maafw_runner``）与两块解析库。规则常量与项目判据在
``rules.py``，排队列的前后两段（活动探测前、后）在 ``arrange.py``。插件声明在 ``plugin.py``，执行外壳在
``expander.py``，属主入口在 ``owner.py``，配置类在 ``config.py``。

``PLUGIN`` 与 SRC 专项一样在导入期给出（加载器在 ``exec_module`` 里接住加载错误）。唯一的例外：进程里根本没有
宿主（``app`` 这个顶层包不存在，例如离线工具或单元测试只用 ``rules`` / ``arrange``）时不导出 ``PLUGIN``，
纯逻辑模块照样能导入；宿主里缺任何子模块（``app.xxx``）照常报错。
"""

try:
    from auto_mas_core import PLUGIN_API_VERSION

    from .plugin import Plugin as PLUGIN
except ModuleNotFoundError as _exc:
    if _exc.name != "app":
        raise
    __all__: list[str] = []
else:
    __all__ = ["PLUGIN", "PLUGIN_API_VERSION"]
