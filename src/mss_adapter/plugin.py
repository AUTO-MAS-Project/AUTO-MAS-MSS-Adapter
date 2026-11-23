#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#   SPDX-License-Identifier: AGPL-3.0-or-later

"""MSS 专项插件声明：托管 / 账号 / 计划表类型、属主服务、执行外壳（取代宿主 ``app/task/MSS/registration.py``）。

规则 L：本模块和它顶层的整条导入链里不出现别的插件包（三块积木、另两个专项）。三块服务插件只经服务调
（``services.SLOT``：启用时绑宿主给的解析器、停用时解）；属主是专项共用库的新形 ``ConfigOwner``（``owner.OWNER``），
经项目服务的 ``owner_register`` 登记；托管生命周期钩子与按配置的判定在 ``hooks`` 里。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields as _dataclass_fields
from typing import Any

from auto_mas_core import Config, ScriptAdapterPlugin, ScriptTypeDecl, TaskMode
from auto_mas_core.host.utils import get_logger
from maafw_adapter_kit.owner_entry import OwnerServiceMixin, owner_provides
from maafw_adapter_kit.services import (
    PROJECT_OWNER_REGISTER,
    PROJECT_OWNER_UNREGISTER,
    owner_register_request,
    owner_unregister_request,
)

from . import hooks as _hooks
from . import owner as _owner
from .config import MSSConfig, MSSPlanConfig, MSSUserConfig
from .expander import MSSExpander, prefetch_check, running_count
from .services import REQUIRES, SLOT, client

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


class Plugin(OwnerServiceMixin, ScriptAdapterPlugin):
    script_type_key = "MSS"
    requires = REQUIRES
    wants = ()
    provides_services = owner_provides(_owner.MSS_OWNER)
    owner_entry = _owner.OWNER
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
        """先绑服务槽（管理器、账号流程、钩子从这里现取服务），再经项目服务登记属主。

        登记只试一次（生命周期钩子里的调用不重试）；登记不上（属主名冲突等）启用失败，同原来直接登记时。
        """
        SLOT.bind(self.services)
        try:
            reply = await client("on_enable").call_once(
                PROJECT_OWNER_REGISTER,
                owner_register_request(_owner.MSS_OWNER, _owner.OWNER_SERVICE),
            )
            reply.expect_ok()
        except BaseException:
            SLOT.unbind()
            raise

    async def on_disable(self) -> None:
        """项目服务还在就注销属主；失败只记 warning。最后解开服务槽。"""
        try:
            # 项目服务已经不在就不用注销（同原来 services.get 拿不到时跳过）
            if self.services.get(PROJECT_OWNER_UNREGISTER) is not None:
                try:
                    reply = await client("on_disable").call_once(
                        PROJECT_OWNER_UNREGISTER,
                        owner_unregister_request(_owner.MSS_OWNER),
                    )
                    if reply.failed:
                        logger.warning(f"MSS 专项注销托管属主失败：{reply.message}")
                except Exception as exc:  # noqa: BLE001 - 停用不因注销失败中断
                    logger.warning(f"MSS 专项注销托管属主失败：{exc}")
        finally:
            SLOT.unbind()

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
