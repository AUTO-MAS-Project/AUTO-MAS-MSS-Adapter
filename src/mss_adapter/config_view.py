#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#   SPDX-License-Identifier: AGPL-3.0-or-later

"""MSS 流程读写配置的唯一入口：只给函数，返回普通值。

``account_run``（账号级）与 ``manager``（脚本级）不直接读脚本配置、账号配置的属性，一律经这里：
它们手里的配置对象只当不透明的句柄，交给这里的函数，或原样交给宿主能力（``close_emulator(self)`` /
``resolve_host_adb(self)`` 读的是任务对象）。以后配置类不再继承 ``MaaFWConfig``、换基类（``UserData`` →
``users``、``Info.Status`` → ``info.enabled``）时，只改这一个模块。

读的时机与通用 MFW 流程（``runner_task`` / ``embedded_manager``）完全相同：每个函数都在调用那一刻现读，
不冻结——账号的 ``Data.*`` 由流程自己写、代理地址与静默模式受全局设置影响，托管级字段虽然在 X 锁里，
运行中的资源包修复仍可能改写导入报告。写字段一律「改一项、提交一次」，次数与先后照旧。
吃配置对象的现成宿主函数（``proxy_times_used_up``、``client_executable_path``、
``fill_default_task_snapshots`` ……）也只在这里调。

MSS 自己的两项：账号引用的计划表今天那一格（:func:`plan_slot`，照旧经 ``related_config["PlanConfig"]``
取，插件化时只改这一处）与「活动优先」的原值（:func:`if_activity_first_raw`）。
"""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from auto_mas_core.devices.task import game_device_id, game_target_id
from auto_mas_core.host.core import Config
from maafw_adapter_kit.backup_archive import read_overlay_values
from maafw_adapter_kit.default_snapshot import fill_default_task_snapshots
from maafw_adapter_kit.option_secrets import (
    script_task_snapshots,
    user_task_snapshot,
)
from maafw_interface.interface.models import MaaFWInterface
from maafw_interface.runner.plan_transforms import MaaFWPeriodLimits
from maafw_interface.runner.run_plan import resolve_run_selection
from maafw_adapter_kit.services import desktop_spec as _desktop_spec_dict
from maafw_native.layout import view_dir
from maafw_adapter_kit.proxy_quota import (
    proxy_times_used_up as _proxy_times_used_up,
)
from maafw_adapter_kit.run_settings import period_limits as _period_limits
from mss_adapter.config import MSSConfig
from mss_adapter.rules import PLAN_MODE_FIXED

# ---------------------------------------------------------------------------
# 宿主函数：经 SDK 出口用到时现取（客户端绑定、游戏目标在 auto_mas_core.host，Unity 分辨率在宿主
# app.utils 的再导出里）
# ---------------------------------------------------------------------------


def client_executable_path(script: Any) -> Any:
    from auto_mas_core.host.task_client import client_executable_path as _impl

    return _impl(script)


def client_launcher_path(script: Any) -> Any:
    from auto_mas_core.host.task_client import client_launcher_path as _impl

    return _impl(script)


def game_is_emulator(owner: Any) -> bool:
    from auto_mas_core.host.task_game import game_is_emulator as _impl

    return _impl(owner)


def parse_resolution_option(value: Any) -> Any:
    from auto_mas_core.host.utils import game_resolution

    return game_resolution.parse_resolution_option(value)


# ---------------------------------------------------------------------------
# 托管（脚本级）
# ---------------------------------------------------------------------------


def lookup_script(script_uid: uuid.UUID) -> Any:
    """宿主托管表里的这个托管；不存在时照原样抛 ``KeyError`` / ``ValueError``。"""

    return Config.ScriptConfig[script_uid]


def is_mss_script(script: Any) -> bool:
    """按确切类型认 MSS 托管（不靠继承）。"""

    return type(script) is MSSConfig


def script_uid(script: Any) -> Any:
    """托管的 uid（``config_locks`` 的键）。"""

    return script.uid


def script_name(script: Any) -> Any:
    """托管名 ``Info.Name``。"""

    return script.Info.Name


def user_collection(script: Any) -> Any:
    """托管的账号集合（插件配置类上是 ``users``，原 ``UserData``）：按 uid 下标取账号配置，不存在时照原样抛
    ``KeyError``。"""

    return script.users


def user_items(users: Any) -> list[tuple[uuid.UUID, Any]]:
    """账号集合里的全部账号：``[(账号 uid, 账号配置), ...]``，按配置里的顺序。"""

    return list(users.data.items())


def user_entries(users: Any) -> Any:
    """账号集合底下的 uid → 账号配置映射（交给账号流程按 uid 取自己的账号）。"""

    return users.data


def project_root(script_id: str, script: Any) -> Path:
    """有效项目根（视图），未 ``resolve()``。有效根不取决于配置，``script`` 只为调用方签名不变。"""

    return view_dir(script_id)


def emulator_target(script: Any) -> str:
    """托管绑定的模拟器（``game_target_id``）；没配是 ``"-"`` 或空。"""

    return game_target_id(script)


def emulator_device(script: Any) -> str:
    """托管绑定的模拟器实例（``game_device_id``）。"""

    return game_device_id(script)


async def fill_default_snapshots(
    script_id: str, script: Any, *, holds_lock: bool = False
) -> None:
    """新账号的默认任务队列（经 ``Config.update_user`` 走插件的保存前钩子，加密照常）。

    ``holds_lock``：宿主 L2 替这一轮持着托管的 X 锁（管理器的 ``_host_locked_config`` 不为 None）。
    共用库带这个参数时照常写；0.3.0 的共用库没有它，就不传（托管锁着时跳过，与原来相同）。按函数
    签名认，不看版本号。
    """

    if (
        holds_lock
        and "holds_lock" in inspect.signature(fill_default_task_snapshots).parameters
    ):
        await fill_default_task_snapshots(script_id, script, holds_lock=True)
    else:
        await fill_default_task_snapshots(script_id, script)


def source_path(script: Any) -> str:
    """导入来源 ``Info.Path``，去掉首尾空白。"""

    return str(script.Info.Path or "").strip()


def run_selection(
    script: Any, interface_model: MaaFWInterface
) -> tuple[str | None, str | None]:
    """``(交给建计划的 controller 名, 实际生效的 resource 名)``，口径见 ``resolve_run_selection``。

    用户在托管页显式选的 controller 优先（为什么不按「配了模拟器」优先，见通用流程
    ``runner_task._select_run_selection`` 的说明）。
    """

    return resolve_run_selection(
        interface_model,
        configured_controller=str(script.Info.Controller or ""),
        emulator_selected=game_is_emulator(script),
        configured_resource=str(script.Info.Resource or ""),
    )


def hotkeys_raw(script: Any) -> Any:
    return script.Game.Hotkeys


def run_times_limit(script: Any) -> Any:
    """``Run.RunTimesLimit`` 原值（日志里的分母照原值写）。"""

    return script.Run.RunTimesLimit


def run_time_limit(script: Any) -> Any:
    return script.Run.RunTimeLimit


def task_time_limit_raw(script: Any) -> tuple[Any, Any]:
    """``(Run.TaskTimeLimit, Run.TaskTimeLimitOverrides)`` 原值。"""

    run = script.Run
    return run.TaskTimeLimit, run.TaskTimeLimitOverrides


def loop_guard_raw(script: Any) -> Any:
    """``Run.LoopGuard`` 的原值（原地打转检测，配置默认开）；读不到按关。"""

    try:
        return script.Run.LoopGuard
    except Exception:
        return None


def period_limits(script: Any) -> MaaFWPeriodLimits:
    run = script.Run
    return _period_limits(run.DailyOnceTasks, run.WeeklyOnceTasks, run.MonthlyOnceTasks)


def game_wait_time(script: Any) -> Any:
    return script.Game.WaitTime


def game_package_name(script: Any) -> Any:
    return script.Game.PackageName


def configured_hwnd(script: Any) -> Any:
    return script.Device.HWnd


def adb_path_raw(script: Any) -> Any:
    return script.Device.AdbPath


def adb_methods_raw(script: Any) -> tuple[Any, Any]:
    """``(Device.AdbScreencapMethods, Device.AdbInputMethods)`` 原值。"""

    device = script.Device
    return device.AdbScreencapMethods, device.AdbInputMethods


def win32_methods_raw(script: Any) -> tuple[Any, Any, Any]:
    """``(Win32ScreencapMethod, Win32MouseMethod, Win32KeyboardMethod)`` 原值。"""

    device = script.Device
    return (
        device.Win32ScreencapMethod,
        device.Win32MouseMethod,
        device.Win32KeyboardMethod,
    )


def all_task_snapshots(script: Any) -> list[Any]:
    """托管下全部账号 ``Task.TaskSnapshot`` 的原值（``.project.log`` 打码用）。"""

    return script_task_snapshots(script)


def desktop_spec(script: Any, *, for_close: bool = False) -> dict[str, Any]:
    """桌面游戏启停用到的配置原值（运行服务桌面游戏操作的 ``spec``，专项共用库 ``desktop_spec`` 拼成的
    字典），每个调用点现取。

    游戏 exe 与启动器来自客户端绑定（``client_executable_path`` / ``client_launcher_path``）。
    ``for_close``：收尾关游戏用。读客户端绑定出错不在这里抛，原文记进 spec 交给运行服务，在原来读路径
    的那一步再报——没开过游戏时根本不读路径，开过的先关进程再报。
    """

    game_path: Path | None = None
    launcher_path: Path | None = None
    game_path_error: Exception | None = None
    launcher_path_error: Exception | None = None
    try:
        raw = str(client_executable_path(script) or "").strip()
        game_path = Path(raw) if raw else None
    except Exception as exc:
        if not for_close:
            raise
        game_path_error = exc
    try:
        launcher_path = client_launcher_path(script)
    except Exception as exc:
        if not for_close:
            raise
        launcher_path_error = exc
    game = script.Game
    return _desktop_spec_dict(
        launch_mode=str(game.LaunchMode or "DirectExe").strip(),
        game_path=game_path,
        launcher_path=launcher_path,
        url=str(game.URL or "").strip(),
        arguments=game.Arguments,
        wait_seconds=game.WaitTime,
        unity_resolution=parse_resolution_option(game.UnityResolution),
        close_mode=str(game.CloseMode or "OnFailure").strip(),
        configured_hwnd=script.Device.HWnd,
        game_path_error=game_path_error,
        launcher_path_error=launcher_path_error,
    )


# ---------------------------------------------------------------------------
# 全局设置
# ---------------------------------------------------------------------------


def silent_mode() -> bool:
    """全局「静默模式」（``Function.IfSilence``）；调用那一刻现读。"""

    return bool(Config.setting.Function.IfSilence)


def client_version() -> str:
    """建计划时写进 ``PI_CLIENT_VERSION`` 的宿主版本号。

    解析库不再自己按文件位置找宿主（``build_arranged_plan(..., client_version=)``），取值照宿主迁移分支的
    ``seams/host_version.host_client_version``：宿主资源目录 ``res/version.json`` 的 ``version``，不是字符串
    或读失败给 ``""``。
    """

    try:
        data = json.loads(
            (Path(Config.resource_path) / "version.json").read_text(encoding="utf-8")
        )
        version = data.get("version")
        return version if isinstance(version, str) else ""
    except Exception:  # noqa: BLE001 - 与原函数同一口径：读不到就给空串
        return ""


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------


def user_name(user: Any) -> Any:
    return user.Info.Name


def runnable(user: Any) -> bool:
    """账号已启用、剩余天数没耗尽（管理器筛账号）。"""

    return bool(user.Info.Status and user.Info.RemainedDay != 0)


def account_emulator_without_device(user: Any) -> bool:
    """账号另绑了模拟器目标、设备却是「-」；目标已不存在时不拦，留给 select 报原因。"""

    binding = user.game
    if binding.game_id == "-" or binding.device_id != "-":
        return False
    target = Config.GameConfig.get(binding.game_id)
    return target is not None and target.info.type == "emulator"


def proxy_times_used_up(user: Any, script: Any, today: str) -> bool:
    """今日代理次数是否已达上限（``proxy_quota``，与通用 MFW 同一判据）。"""

    return _proxy_times_used_up(user, script, today)


def overlay_values(user: Any) -> dict:
    """账号 MAS 字段侧车的值（运行前归档用）。"""

    return read_overlay_values(user)


def plan_slot(user: Any, log: Callable[[str], None]) -> dict[str, Any] | None:
    """账号引用的 MSS 计划表今天那一格；``Fixed`` 或取不到时返回 None（本轮不改悬赏试炼）。

    ``Info.PlanMode`` 读不到按 ``Fixed``。计划表照旧经账号类的 ``related_config["PlanConfig"]``
    按 uuid 取（被删、类型不对时写一行账号日志、按固定处理，文案照旧）；今天那一格不是对象、或没有关卡
    时同样按固定处理。
    """

    mode = str(_value(user, "Info", "PlanMode") or PLAN_MODE_FIXED)
    if mode == PLAN_MODE_FIXED:
        return None
    try:
        plan = type(user).related_config["PlanConfig"][uuid.UUID(mode)]
        key = plan.get_current_key()
    except Exception as exc:  # noqa: BLE001 - 计划表被删或类型不对：按固定处理
        log(f"[MSS] 引用的计划表不可用，本轮不改悬赏试炼：{exc}")
        return None
    if not isinstance(key, dict) or not str(key.get("TribulationStage") or ""):
        return None
    return key


def if_activity_first_raw(user: Any) -> Any:
    """``Info.IfActivityFirst`` 的原值（只有 ``False`` 算关）；读不到按 None。"""

    return _value(user, "Info", "IfActivityFirst")


def task_snapshot_raw(user: Any) -> Any:
    return user.Task.TaskSnapshot


def task_snapshot_secret_raw(user: Any | None) -> Any | None:
    """打码用的快照原值（``option_secrets.user_task_snapshot``：没有账号、读不出来都是 None）。"""

    return user_task_snapshot(user)


def selected_preset(user: Any) -> str:
    return str(user.Task.SelectedPreset or "").strip()


def script_before_task(user: Any) -> tuple[bool, Any]:
    """``(Info.IfScriptBeforeTask, Info.ScriptBeforeTask)``；开关关着时不读路径。"""

    info = user.Info
    if not info.IfScriptBeforeTask:
        return False, None
    return True, info.ScriptBeforeTask


def script_after_task(user: Any) -> tuple[bool, Any]:
    """``(Info.IfScriptAfterTask, Info.ScriptAfterTask)``；开关关着时不读路径。"""

    info = user.Info
    if not info.IfScriptAfterTask:
        return False, None
    return True, info.ScriptAfterTask


def proxy_times(user: Any) -> Any:
    return user.Data.ProxyTimes


def remained_day(user: Any) -> Any:
    return user.Info.RemainedDay


def last_proxy_date(user: Any) -> Any:
    return user.Data.LastProxyDate


def period_records_raw(user: Any) -> Any:
    return user.Data.PeriodTaskRecords


async def set_last_proxy_date(user: Any, value: Any) -> None:
    user.Data.LastProxyDate = value
    await user.commit()


async def set_proxy_times(user: Any, value: int) -> None:
    user.Data.ProxyTimes = value
    await user.commit()


async def set_remained_day(user: Any, value: int) -> None:
    user.Info.RemainedDay = value
    await user.commit()


async def set_last_proxy_status(user: Any, value: str) -> None:
    user.Data.LastProxyStatus = value
    await user.commit()


async def set_period_records(user: Any, value: str) -> None:
    user.Data.PeriodTaskRecords = value
    await user.commit()


def _value(config: Any, group: str, name: str) -> Any:
    """``config.<group>.<name>``；缺分组或缺字段时返回 None（假配置对象缺项时按空处理）。"""

    try:
        return getattr(getattr(config, group), name)
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "adb_methods_raw",
    "adb_path_raw",
    "all_task_snapshots",
    "client_version",
    "configured_hwnd",
    "desktop_spec",
    "emulator_device",
    "emulator_target",
    "fill_default_snapshots",
    "game_package_name",
    "game_wait_time",
    "hotkeys_raw",
    "if_activity_first_raw",
    "is_mss_script",
    "last_proxy_date",
    "lookup_script",
    "loop_guard_raw",
    "overlay_values",
    "period_limits",
    "period_records_raw",
    "plan_slot",
    "project_root",
    "proxy_times",
    "proxy_times_used_up",
    "remained_day",
    "run_selection",
    "run_time_limit",
    "run_times_limit",
    "runnable",
    "script_after_task",
    "script_before_task",
    "script_name",
    "script_uid",
    "selected_preset",
    "set_last_proxy_date",
    "set_last_proxy_status",
    "set_period_records",
    "set_proxy_times",
    "set_remained_day",
    "silent_mode",
    "source_path",
    "task_snapshot_raw",
    "task_snapshot_secret_raw",
    "task_time_limit_raw",
    "user_collection",
    "user_entries",
    "user_items",
    "user_name",
    "win32_methods_raw",
]
