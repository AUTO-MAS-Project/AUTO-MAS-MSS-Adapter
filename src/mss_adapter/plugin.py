#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#   SPDX-License-Identifier: AGPL-3.0-or-later

"""MSS 专项插件声明：托管 / 账号 / 计划表类型、属主服务、执行外壳（取代宿主 ``app/task/MSS/registration.py``）。

规则 L：本模块和它顶层的整条导入链里不出现别的插件包（三块积木、另两个专项）。项目更新插件只经服务
``maafw.project.v1`` 调；属主入口在 ``owner.build_owner`` 里现导入它的 ``glue.owner_entry``。
"""

from __future__ import annotations

from typing import Any

from auto_mas_core import Config, ScriptAdapterPlugin, ScriptTypeDecl, TaskMode
from auto_mas_core.host.utils import get_logger

from . import owner as _owner
from .config import MSSConfig, MSSPlanConfig, MSSUserConfig
from .expander import MSSExpander, prefetch_check

logger = get_logger("MSS 专项")

# 宿主还内置 MSS 时，同名类型会冲突（add_type 直接抛错）：拒绝加载，提示换到让位分支。
_builtin = Config.ScriptConfig.entry_types.get("MSSConfig")
if _builtin is not None and _builtin.__module__.startswith("app."):
    raise ImportError("当前 AUTO-MAS 仍内置 MFW / M9A / MSS，请换到让位分支")

PROJECT_SERVICE = "maafw.project.v1"


class Plugin(ScriptAdapterPlugin):
    script_type_key = "MSS"
    requires = ("maafw.agent_env.v1", "maafw.runner.v1", PROJECT_SERVICE)
    wants = ()
    provides_services = {_owner.OWNER_SERVICE: "owner_entry"}
    decl = ScriptTypeDecl(
        script_type=MSSConfig,
        user_type=MSSUserConfig,
        expander_class=MSSExpander,
        plan_type=MSSPlanConfig,
        supported_modes=(TaskMode.AUTO_PROXY,),
        prefetch_check=prefetch_check,
        game_kinds=("emulator", "client"),
        supports_mod_avd=True,
        activity_game="stellasora",
    )

    def __init__(self, config: Any = None, record: Any = None) -> None:
        super().__init__(config=config, record=record)
        self._owner: Any = None

    async def pre_enable(self) -> None:
        """挂关联集合：账号的计划表引用（``Info.PlanMode``）与 ``config_view.plan_slot`` 都经它取。"""
        refs = {
            "PlanConfig": Config.PlanConfig,
            "games": Config.GameConfig,
            "ScriptConfig": Config.ScriptConfig,
        }
        MSSConfig.related_config = refs
        MSSUserConfig.related_config = refs

    async def on_enable(self) -> None:
        """建属主入口并登记给项目更新插件（它每次经服务名现取入口）。"""
        self._owner = _owner.build_owner()
        await self.services.require(PROJECT_SERVICE)(
            "owner.register", name=_owner.MSS_OWNER, service=_owner.OWNER_SERVICE
        )

    async def on_disable(self) -> None:
        """服务还在就注销属主；失败只记 warning。"""
        service = self.services.get(PROJECT_SERVICE)
        if service is not None:
            try:
                await service("owner.unregister", name=_owner.MSS_OWNER)
            except Exception as exc:  # noqa: BLE001 - 停用不因注销失败中断
                logger.warning(f"MSS 专项注销托管属主失败：{exc}")
        _owner.drop_owner()
        self._owner = None

    def owner_entry(self) -> Any:
        """属主服务：返回本包的属主入口对象（项目更新插件调用，不带参数）。"""
        return self._owner


__all__ = ["Plugin"]
