#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#   SPDX-License-Identifier: AGPL-3.0-or-later

"""MSS 的托管属主（取代宿主 ``app/task/MSS/registration.py`` 里属主那一半）。

:data:`OWNER` 是新形属主（专项共用库的 ``ConfigOwner``），按确切类型 ``MSSConfig`` 认领 MSS 托管：插件混入
``OwnerServiceMixin``，项目服务按服务名前缀 ``maafw.owner.mss_adapter.v1`` 现取它的 8 个操作；管理器读托管
记录、写回项目记录也直接用它（本地调，不绕道项目服务）。属主名照旧是 ``mss_adapter``。

规则 L：本模块顶层只导入标准库、解析库、专项共用库的 ``owner_entry``（它顶层只导入解析库）和本包的
``rules``；配置类在函数里现导入。
"""

from __future__ import annotations

from typing import Any

from maafw_adapter_kit.owner_entry import ConfigOwner, owner_service_prefix
from maafw_interface.interface.plan_rules import EMPTY_PLAN_RULES

from .rules import TYPE_KEY, is_mss_project

MSS_OWNER = "mss_adapter"
#: 属主服务名前缀（``maafw.owner.mss_adapter.v1``），8 个操作挂在它下面
OWNER_SERVICE = owner_service_prefix(MSS_OWNER)

# 原文取自 api_service/embedded.py 的 _project_flavor_mismatch（type_key 为 MSS），一字不改
MISMATCH_MESSAGE = f"这不是 {TYPE_KEY} 项目。要用别的项目，请新建通用 MFW 托管。"


def project_mismatch(interface: Any) -> str | None:
    """MSS 托管要换进来的项目不是 MSS 时的拒收原因；能收返回 None。

    识别本身出错不拦（与原来 ``_project_flavor_mismatch`` 同一口径，记一行 warning）。
    """

    try:
        if is_mss_project(interface):
            return None
    except Exception as exc:  # noqa: BLE001 - 识别出错不该挡住导入
        from auto_mas_core.host.utils import get_logger

        get_logger("MFW 内置运行").warning(
            f"MaaFW 特调 {TYPE_KEY} 识别项目失败，不拦截：{exc}"
        )
        return None
    return MISMATCH_MESSAGE


def _config_type() -> type:
    from .config import MSSConfig

    return MSSConfig


#: 本插件名下托管的属主：项目服务经属主服务现取，按确切类型认领 ``MSSConfig``；MSS 没有规则表
OWNER = ConfigOwner(
    MSS_OWNER,
    _config_type,
    plan_rules=EMPTY_PLAN_RULES,
    project_mismatch=project_mismatch,
)


__all__ = [
    "MISMATCH_MESSAGE",
    "MSS_OWNER",
    "OWNER",
    "OWNER_SERVICE",
    "project_mismatch",
]
