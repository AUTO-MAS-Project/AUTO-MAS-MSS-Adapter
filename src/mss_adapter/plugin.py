#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#   SPDX-License-Identifier: AGPL-3.0-or-later

"""MSS 专项插件声明：托管 / 账号 / 计划表类型、属主服务、执行外壳（取代宿主 ``app/task/MSS/registration.py``）。

规则 L：本模块和它顶层的整条导入链里不出现别的插件包（三块积木、另两个专项）。项目更新插件只经服务
``maafw.project.v1`` 调；属主入口在 ``owner.build_owner`` 里现导入专项共用库的 ``owner_entry``；托管生命周期
钩子与按配置的判定在 ``hooks`` 里，积木与共用库都在函数里现导入。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields as _dataclass_fields
from typing import Any

from auto_mas_core import Config, ScriptAdapterPlugin, ScriptTypeDecl, TaskMode
from auto_mas_core.host.utils import get_logger

from . import hooks as _hooks
from . import owner as _owner
from .config import MSSConfig, MSSPlanConfig, MSSUserConfig
from .expander import MSSExpander, prefetch_check, running_count

logger = get_logger("MSS 专项")

# 宿主还内置 MSS 时，同名类型会冲突（add_type 直接抛错）：拒绝加载，提示换到让位分支。
_builtin = Config.ScriptConfig.entry_types.get("MSSConfig")
if _builtin is not None and _builtin.__module__.startswith("app."):
    raise ImportError("当前 AUTO-MAS 仍内置 MFW / M9A / MSS，请换到让位分支")

# 托管生命周期钩子、按配置判定游戏类型与桌面分身是 SDK 后补的出口；旧宿主上钩子不会被调用（新账号没有默认
# 队列、保存时任务快照里的密码不加密），所以没有就拒绝加载。
if not hasattr(ScriptAdapterPlugin, "on_script_cloned") or not {
    "game_kind",
    "desktop_session",
} <= {field.name for field in _dataclass_fields(ScriptTypeDecl)}:
    raise ImportError(
        "当前 AUTO-MAS 版本过旧，缺少 MSS 专项要用的托管生命周期钩子，请升级 AUTO-MAS"
    )

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
        game_kind=_hooks.game_kind,
        desktop_session=_hooks.desktop_session,
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

    async def teardown_blockers(self) -> list[str]:
        """还有 MSS 托管在跑时拒绝停用、重载、卸载（运行里直接用着本包和积木的模块）。"""
        count = running_count()
        return (
            [f"还有 {count} 个 MSS 托管正在运行，等它们结束后再操作"] if count else []
        )

    # ── 托管生命周期钩子（宿主经本插件的调用门调，实现见 hooks） ──

    async def prepare_user_update(
        self, script: Any, user: Any, data: dict[str, Any]
    ) -> Callable[[], None] | None:
        await _hooks.prepare_user_update(script, user, data)
        return None

    async def prepare_user_read(self, script: Any, user_ids: list[str] | None) -> None:
        await _hooks.prepare_user_read(script, user_ids)

    async def on_user_added(self, script: Any, user: Any) -> None:
        await _hooks.on_user_added(script, user)

    async def on_script_cloned(self, source: Any, target: Any) -> None:
        await _hooks.on_script_cloned(source, target)

    async def on_script_removed(self, script_id: str) -> None:
        await _hooks.on_script_removed(script_id)


__all__ = ["Plugin"]
