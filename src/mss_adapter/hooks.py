#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#   SPDX-License-Identifier: AGPL-3.0-or-later

"""MSS 托管的生命周期钩子与按配置的判定（``plugin.Plugin`` 的钩子方法与 ``ScriptTypeDecl`` 调这里）。

内置时这些事由宿主给 MFW 家族绑的配置回调做（宿主迁移分支 ``app/task/MaaFW/seams/config_handler.py``、
``script_lifecycle.py``，MSS 用通用版、属主名 ``mss_adapter``），插件版经 SDK 的托管生命周期钩子接上，
做的事与先后照旧：

- 保存账号前（``prepare_user_update``）：全局选项双写、任务快照里的密码字段加密（专项共用库 ``user_update``）；
  MSS 没有保存前的专项整理。
- 读账号前（``prepare_user_read``）、新账号提交后（``on_user_added``）：空任务队列按项目默认写成实打实的队列
  （专项共用库 ``default_snapshot``；项目还没导入时什么都不写，留到下一次）。
- 复制托管（``on_script_cloned``）：关掉目标的开发者模式，按来源挂的载荷给目标建视图、当场记下本属主。
- 删除托管（``on_script_removed``）：删掉这个托管的视图（视图记着别的属主或别的托管时不删），再起一轮运行池对账。
- ``game_kind`` / ``desktop_session``：按托管所选控制器判定可绑的游戏类型、能不能进桌面分身（宿主
  ``config_handler.expected_game_kind`` 与 ``desktop_session/lifecycle.supports_task`` 里 MFW 那一段）。

规则 L：本模块顶层只导入标准库、SDK 与本包的轻量模块；项目更新插件、专项共用库与解析库都在函数里现导入。
"""

from __future__ import annotations

import asyncio
from typing import Any

from auto_mas_core.host.utils import get_logger

from .owner import MSS_OWNER

logger = get_logger("配置管理")


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------


async def prepare_user_update(script: Any, user: Any, data: dict[str, Any]) -> None:
    """保存账号前原地准备补丁：全局选项双写、快照密码加密（补丁里没有任务快照时不动）。"""

    from maafw_adapter_kit.user_update import prepare_user_update as _impl

    await _impl(script, user, data=data)


async def prepare_user_read(script: Any, user_ids: list[str] | None) -> None:
    """读账号前把空任务队列补成项目默认队列（``user_ids`` 为 None 表示整个托管）。从不抛出。"""

    from maafw_adapter_kit.default_snapshot import prepare_user_read as _impl

    await _impl(script, user_ids=user_ids)


async def on_user_added(script: Any, user: Any) -> None:
    """新账号的空任务队列按项目默认写回；项目未就绪时留到读取、运行前再补。从不抛出。"""

    from maafw_adapter_kit.default_snapshot import fill_default_task_snapshots

    await fill_default_task_snapshots(str(script.uid), script, [str(user.uid)])


# ---------------------------------------------------------------------------
# 托管
# ---------------------------------------------------------------------------


async def on_script_cloned(source: Any, target: Any) -> None:
    """复制托管：建目标视图；失败留待运行前重建，不影响已提交的复制。

    开发者模式不随复制继承，目标挂来源原渠道组的 latest。组里没有正式载荷，或来源是源码形态时先不建视图，
    首次运行按来源目录导入。新视图当场记下本属主（``mss_adapter``）。
    """

    from maafw_project_update.host.embedded_project import (
        FollowSourceCloneUnavailable,
        clone_embedded_copy,
        embedded_project_dir,
        is_source_form,
    )
    from maafw_project_update.host.project_path import (
        release_project_path,
        try_reserve_project_path,
    )

    await target.update(
        type(target).model_validate({"Embedded": {"FollowSource": False}}),
        check_visibility=False,
    )
    group_channel = str(source.Update.Channel or "stable")
    source_form = is_source_form(source)

    target_key = await try_reserve_project_path(embedded_project_dir(str(target.uid)))
    if target_key is None:
        return
    try:
        await asyncio.to_thread(
            lambda: clone_embedded_copy(
                source_script_id=str(source.uid),
                target_script_id=str(target.uid),
                group_channel=group_channel,
                source_form=source_form,
                owner=MSS_OWNER,
            )
        )
    except FollowSourceCloneUnavailable as exc:
        logger.info(f"复制托管时先不建视图，第一次运行前按来源目录导入: {exc}")
    except Exception as exc:  # noqa: BLE001 - 失败下次运行会按来源 / 载荷重建
        logger.warning(f"复制托管时建视图失败，将按需重建: {exc}")
    finally:
        await release_project_path(target_key)


async def on_script_removed(script_id: str) -> None:
    """删除托管：宿主删完 ``data/<uid>`` 之后删本托管的视图，再起一轮运行池对账（不等结果）。

    视图不放在 ``data/<uid>/`` 下（免得进整目录备份）。视图上记着别的属主、或记着别的托管（目录名只取
    uuid 前 12 位，撞上时共用）时不删，记一句。
    """

    from auto_mas_core.host.utils import io as _io
    from maafw_project_update.host.embedded_project import (
        embedded_project_dir,
        view_owner,
    )
    from maafw_project_update.host.live_projects import reconcile_in_background

    embedded_copy = embedded_project_dir(script_id)
    if embedded_copy.exists():
        recorded = await asyncio.to_thread(view_owner, embedded_copy)
        if recorded is not None and (
            recorded[0] != MSS_OWNER or recorded[1].lower() != str(script_id).lower()
        ):
            logger.warning(
                f"删除托管 {script_id} 时视图记着 {recorded[0]} / {recorded[1]}，不是本托管，不删: "
                f"{embedded_copy}"
            )
        else:
            await asyncio.to_thread(_io.force_rmtree, embedded_copy)
    reconcile_in_background("script-deleted")


# ---------------------------------------------------------------------------
# 按配置的判定（同步，在事件循环里执行，只做轻量读取）
# ---------------------------------------------------------------------------


def game_kind(script: Any, patch: dict[str, Any]) -> str | None:
    """所选控制器决定可绑的游戏类型：Adb 是 ``"emulator"``，其余是 ``"client"``。

    控制器取本次补丁里的 ``Info.Controller``，没有就取托管当前的；没选控制器、项目还没导入、interface 里没有
    这个控制器时返回 None（按静态的 ``game_kinds``，不拦）。
    """

    from maafw_interface.interface.loader import load_interface_model_cached
    from maafw_project_update.host.embedded_project import resolve_maafw_project_root

    info = patch.get("Info") if isinstance(patch, dict) else None
    controller = (
        info.get("Controller", script.Info.Controller)
        if isinstance(info, dict)
        else script.Info.Controller
    )
    if not controller:
        return None
    root = resolve_maafw_project_root(str(script.uid), script)
    if not root.is_dir():
        return None
    interface = load_interface_model_cached(root)
    selected = next(
        (item for item in interface.controller if item.name == controller), None
    )
    if selected is None:
        return None
    return "emulator" if selected.type == "Adb" else "client"


def desktop_session(script: Any) -> bool:
    """按运行时会选的控制器判定：是 Win32 才能进桌面分身；项目读不出来时不支持。"""

    from auto_mas_core.host.task_game import game_is_emulator
    from maafw_interface.interface.service import MaaFWInterfaceService
    from maafw_interface.runner.run_plan import (
        _select_controller,
        resolve_run_selection,
    )
    from maafw_project_update.host.embedded_project import resolve_maafw_project_root

    try:
        interface = MaaFWInterfaceService().load(
            resolve_maafw_project_root(str(script.uid), script)
        )
        name, _ = resolve_run_selection(
            interface,
            configured_controller=str(script.Info.Controller),
            emulator_selected=game_is_emulator(script),
            configured_resource=str(script.Info.Resource),
        )
        return _select_controller(interface, name).type == "Win32"
    except (OSError, ValueError):
        return False


__all__ = [
    "desktop_session",
    "game_kind",
    "on_script_cloned",
    "on_script_removed",
    "on_user_added",
    "prepare_user_read",
    "prepare_user_update",
]
