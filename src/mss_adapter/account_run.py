#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#   SPDX-License-Identifier: AGPL-3.0-or-later

"""MSS 一个账号这一轮的运行（账号级流程）。

由 ``manager.MSSManager`` 每个账号建一个、直接调 ``main_task``，收尾经 ``_run_final_task``。
写法照通用 MFW 的账号流程（``MaaFW/tools/embedded/runner_task``）逐段对应，差别只在 MSS 自己的几步：

- 建计划分三段，都包在 ``check`` 原来那个 shield 循环里（用户这时停止，照样等这一整段做完）：
  工作线程里读 interface、展开任务快照（规则表是空表）、取计划表今天那一格、``arrange.mss_arrange_head``；
  回到事件循环，前半段说要查时才 ``await stella.has_running_official_activity()``（每次运行最多查一次，
  出错同样算「无法构建运行计划」）；再进工作线程 ``arrange.mss_arrange_tail``、按排好的队列建计划、周期过滤、
  桌面检查。日志先后与一次排完时一字不差。活动探测在事件循环上查（旧的风味路径在工作线程里查），
  这是与通用流程跑 MSS 时唯一的差别；
- 没有运行前登录、游戏更新与登录交给首任务（ADB 也照样能跑：``ask_toolkit`` 恒为假，设备配置不插步骤）；
- 重试：剔除本次运行已完成的任务用 ``MSS_SKIP_ON_RETRY``。

跑计划、运行前设置、判定、记账、桌面游戏启停、开模拟器、文案与通知正文都调 ``MaaFW/tools/embedded``
里的叶子，只传普通数据；读写配置一律经 ``config_view``。日志、进度点、历史与通知的先后与通用流程完全相同
（logger 名、``lease_owner`` 也照旧）。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping

from auto_mas_core.host.core import Notify, Publisher
from auto_mas_core.host.core_notify_task_content import (
    NOTIFY_SCREENSHOT_LIMIT,
    prepare_screenshot_images,
    screenshot_entries,
)
from auto_mas_core.host.core_ws import protocol
from auto_mas_core.host.models_emulator import DeviceBase, DeviceInfo
from auto_mas_core.host.models_notification import LocalNotice
from auto_mas_core.host.models_schema import (
    HistoryAttachmentInput,
    HistorySnapshotStatistic,
    HistoryWrite,
    WSTaskNoticeData,
)
from auto_mas_core.host.models_task import LogRecord, ScriptItem, TaskExecuteBase
from auto_mas_core.host.services import execute_script_task
from auto_mas_core.host.task_emulator_core import close_emulator
from maafw_interface.controller_win32.service import (
    WIN32_NO_WINDOW_RULES_MESSAGE,
    controller_has_window_rules,
    find_controller,
)
from maafw_interface.interface.agent_command import MaaFWAgentCommandPlan
from maafw_interface.interface.models import MaaFWInterface
from maafw_interface.interface.plan_rules import EMPTY_PLAN_RULES
from maafw_interface.interface.preview import interface_display_name
from maafw_interface.interface.service import MaaFWInterfaceService
from maafw_native.project_runtime import (
    describe_project_runtime_architecture_mismatch,
)
from maafw_interface.run_redaction import (
    log_redaction_notice,
    project_log_secret_variants,
    run_secret_values,
    secret_log_variants,
)
from maafw_interface.runner.models import (
    SERVER_MAINTENANCE,
    MaaFWRunPlan,
    MaaFWRunResult,
)
from maafw_interface.runner.plan_transforms import (
    PeriodRecords,
    filter_period_once_tasks,
    parse_period_records,
    period_records_after,
    skip_tasks_completed_this_run,
)
from maafw_interface.runner.resource_failure import describe_resource_failure
from maafw_interface.runner.run_events import (
    CATEGORY_SIGNAL,
    STAGE_CLIENT,
    STAGE_PRETASK,
)
from maafw_interface.runner.run_plan import MaaFWRunPlanError
from maafw_interface.runner.task_selection import (
    MaaFWSnapshotExpansion,
    MaaFWSnapshotInspection,
    build_arranged_plan,
    describe_plan_failure,
    expand_task_snapshot,
    inspect_task_snapshot,
)
from maafw_adapter_kit.attempt_ledger import MaaFWAttemptLedger
from maafw_adapter_kit.device_state import (
    MaaFWAdbDeviceState,
    choose_game_package,
    optional_int,
)
from maafw_adapter_kit.host_client import (
    WORKSTATION_LOCKED_MESSAGE,
    open_emulator_for_run,
    script_adb_env,
    workstation_lock_reason,
)
from maafw_adapter_kit.option_secrets import (
    DPAPI_SECRET_OPENER,
    open_task_snapshot,
)
from maafw_adapter_kit.owner_entry import record_payload
from maafw_adapter_kit.proxy_quota import (
    proxy_day_rolled,
    proxy_today,
    success_settlement,
)
from maafw_adapter_kit.services import (
    PROJECT_CONTEXT,
    PROJECT_REPAIR,
    PROJECT_RUN_RESERVE,
    RUNNER,
    RUNNER_ACCOUNT_CLOSE,
    RUNNER_ACCOUNT_OPEN,
    RUNNER_DESKTOP_CHECK,
    RUNNER_DESKTOP_LAUNCH,
    RUNNER_DESKTOP_QUIT,
    RUNNER_DEVICE_ADB_CONFIG,
    RUNNER_DEVICE_ADB_PATH,
    RUNNER_DEVICE_WIN32_CONFIG,
    RUNNER_PLAN,
    RUNNER_PRETASK,
    RUNNER_PRETASK_KILL,
    RUNNER_PRETASK_RERUN_CHECK,
    Operation,
    Reply,
    ServiceClient,
    ServiceUnavailable,
    account_open_request,
    account_request,
    desktop_check_request,
    desktop_launch_request,
    desktop_quit_request,
    device_adb_config_request,
    device_adb_path_request,
    device_win32_config_request,
    event_dispatcher,
    plan_request,
    pretask_request,
    pretask_rerun_check_request,
    project_context_request,
    release_run_reservation,
    repair_request,
    script_request,
    unavailable_message,
)
from maafw_adapter_kit.run_messages import (
    MISSING_TASK_NOTICE_PREFIX,
    completed_result_log_lines,
    describe_failed_result,
    format_completed_task_labels,
    format_run_overview_log,
    format_user_log_line,
    period_all_done_message,
    record_event_point,
    remaining_done_message,
    result_task_labels,
    retry_skip_log_lines,
    signal_attempt_facts,
)
from maafw_adapter_kit.run_notices import (
    statistics_mark,
    statistics_title,
    success_notice_text,
)
from maafw_adapter_kit.run_record import RUN_RECORD_HISTORY_KEY
from maafw_adapter_kit.run_settings import (
    load_json_dict,
    load_script_hotkeys,
    loop_guard,
    task_time_limits,
)
from maafw_adapter_kit.run_verdict import (
    VERDICT_SIGNAL,
    VERDICT_SUCCEEDED,
    MaaFWWorkstationLocked,
    judge_attempt,
)
from maafw_adapter_kit.signal_notice import (
    MAINTENANCE_LAST_STATUS,
    MAINTENANCE_SKIP_MESSAGE,
    SIGNAL_USER_MESSAGES,
    MaaFWSignalTracker,
)
from mss_adapter import config_view
from mss_adapter.arrange import (
    MSSArrangeState,
    mss_arrange_head,
    mss_arrange_tail,
)
from mss_adapter.rules import MSS_SKIP_ON_RETRY
from mss_adapter.services import client as service_client
from auto_mas_core.adapters.script import begin_attempt, point
from auto_mas_core.host.utils import UTC4, get_logger, power
from auto_mas_core.host.utils_common_process_scope import current_process_scope

# 与通用 MFW 的账号流程同名：app.log 的检索习惯不变
logger = get_logger("MaaFW 插件自动代理")


def _format_maafw() -> Any:
    """宿主的 MFW 统计通知格式（``app.core.notify.formats.maafw``，SDK 未公开）：用到时现导入。"""

    import app.core.notify.formats.maafw as module

    return module


def _desktop_process_target() -> Any:
    """宿主的桌面分身进程目标（``ContextVar``，经 SDK 出口）：用到时现取。"""

    from auto_mas_core.host.utils_common_process_session import desktop_process_target

    return desktop_process_target


class MSSAccountRun(TaskExecuteBase):
    """MSS 一个账号这一轮的 AutoProxy 运行。"""

    def __init__(
        self,
        script_info: ScriptItem,
        script_config: Any,
        user_config: Mapping[uuid.UUID, Any],
        emulator_manager: DeviceBase | None,
        project_update_logs: list[str] | None = None,
        signal_tracker: MaaFWSignalTracker | None = None,
    ):
        super().__init__()

        if script_info.task_info is None:
            raise RuntimeError("ScriptItem 未绑定 TaskItem")

        self.task_info = script_info.task_info
        self.script_info = script_info
        # 配置对象只当句柄：读写经 config_view；close_emulator / resolve_host_adb 按任务对象读它
        self.script_config = script_config
        self.user_config = user_config
        self.emulator_manager = emulator_manager
        self.project_update_logs = list(project_update_logs or [])
        self.cur_user_item = self.script_info.user_list[self.script_info.current_index]
        self.cur_user_uid = uuid.UUID(self.cur_user_item.user_id)
        self.cur_user_config = self.user_config[self.cur_user_uid]
        self.project_path = config_view.project_root(
            str(self.script_info.script_id), self.script_config
        ).resolve()
        self.interface_model: MaaFWInterface | None = None
        self.base_run_plan: MaaFWRunPlan | None = None
        self.run_plan: MaaFWRunPlan | None = None
        # 本账号这一轮第一次借运行环境时 agent 环境算出的 agent 命令计划，重试沿用
        self._worker_agent_plans: tuple[MaaFWAgentCommandPlan, ...] | None = None
        self.cur_user_log: LogRecord | None = None
        # 运行前检查时本账号的日志还没建（排队列的提示就在这时候到），先攒在这里，
        # prepare() 建好日志时并进去
        self._pending_user_log: list[str] = []
        # user_start_time 是本账号这一轮的开始（统计通知用），cur_user_log_started_at 是当前
        # 这次尝试的开始（日志记录与 history 文件名用）
        self.user_start_time: datetime | None = None
        self.cur_user_log_started_at: datetime | None = None
        # on_crash 记下的异常原文：还没进第一次尝试就崩了时，推送靠它说原因
        self._crash_message: str | None = None
        # 已记进账号日志与 history 状态的那个异常（_mark_user_crashed）
        self._crash_marked: Exception | None = None
        # 当前这次尝试给用户看的称呼（「第 2 次尝试」「清理残留后的重新运行」）
        self._attempt_title: str | None = None
        self._history_temp = tempfile.TemporaryDirectory(prefix="mas-maafw-run-")
        self._history_saved = False
        # final_task 里存历史之前写的账号日志先压住（见 final_task），存完再写
        self._held_publishes: list[Any] | None = None
        self._user_finalized = False
        self._user_teardown_pending = False
        self._notification_screenshots: list[tuple[str, bytes]] = []
        self.check_result = "-"
        self.curdate = proxy_today()
        self.run_complete = False
        # 本账号运行期间的「保持显示开启」请求，见 main_task。
        self._display_hold: Any = None
        self.opened_emulator = False
        # 运行服务的账号句柄（account_open，运行前检查一开头开，final_task 最外层关）：Win32 桌面游戏
        # （开没开过、游戏刚启动的时刻、临时改过的 Unity 分辨率、AttachOnly 兜底关闭的分界）与运行前设置
        # （停止与清理的状态）都在运行服务那边、跨尝试保留。这里只记桌面游戏作业回报的开没开过（重试前
        # 要不要关、要不要重跑运行前设置）。
        self._account: str | None = None
        # 收尾抛出时账号句柄留给随后的 on_crash 关（见 final_task）
        self._close_account_after_crash = False
        self._desktop_opened = False
        # 服务作业的事件（日志、进度点、运行记录、阶段）分派回本账号的日志与记账
        self._on_service_event = event_dispatcher(
            log=self._on_service_log,
            point=self._on_service_point,
            record=self._on_run_record_event,
            stage=self._on_service_stage,
        )
        # 模拟器路径上本轮游戏刚启动的单调时钟时刻；None 表示游戏早就在跑。桌面路径的那个时刻在运行
        # 服务的账号里记着。
        self._emulator_started_at: float | None = None
        # 模拟器冷启动游戏时为真：首任务闸门改按读盘判据放行。桌面路径始终为假。
        self._disk_quiet = False
        self.project_lock_key: str | None = None
        # 正在跑的那一份计划（运行服务的 plan 作业）：停止时经它结束 worker
        self._plan_op: Operation | None = None
        # ADB 地址、路径与控制档案（运行服务 device_adb_config 回的字典）：同一次模拟器启动内只解析
        # 一次，关模拟器时清
        self._adb_state = MaaFWAdbDeviceState()
        self.maafw_runtime_pool_root: Path | None = None
        self.maafw_runtime_pool_id: str | None = None
        # 本轮（整个脚本）的信号账，管理器传入共享的一份。
        self.signal_tracker = signal_tracker or MaaFWSignalTracker()
        # 本账号这次运行命中的信号（worker 回报），决定收尾怎么结算。
        self.run_signal: str | None = None
        # 运行前检查发现同一资源本轮已确认在维护、本账号没启动就跳过。
        self.maintenance_skipped = False
        # 运行前检查时项目预约被占，本账号没跑成。
        self.project_busy_skipped = False
        # 队列里 interface 已没有的任务名（项目更新改了 name），建计划时填，进任务报告
        self.missing_task_names: list[str] = []
        # 本次运行里各次尝试已完成的任务名（累计，按份数）：重试时剔掉 MSS_SKIP_ON_RETRY 里的
        self._completed_this_run: list[str] = []
        # 已完成过、但在队列里有多份同名的任务名：重试时不剔、全部重跑，开头写一行说明
        self._retry_rerun_duplicates: list[str] = []
        # 本次尝试的原生 ERR 行：资源包加载失败时据此说出具体原因
        self._attempt_native_errors: list[str] = []
        # 资源包加载失败后的发行包核对（项目服务的 repair 作业）：每个账号每次运行只做一次
        # （``_repair_checked``）；用户停止时还在跑的那个作业交出来、留给交回项目预约时等（``_repair_op``）
        self._repair_checked = False
        self._repair_op: Operation | None = None
        self._extra_attempts = 0
        # 各次尝试的记账（attempt_ledger），键是这次尝试的日志记录 id(cur_user_log)
        self._ledger = MaaFWAttemptLedger()

    # ------------------------------------------------------------------
    # 运行前检查
    # ------------------------------------------------------------------

    async def _open_account(self) -> None:
        """开运行服务的账号句柄（桌面游戏与运行前设置两个对象在那边建，同原来任务构造时建）。

        ``run_started_at``（AttachOnly 兜底关闭的分界）取此刻：契约里只能在开账号时给（原来在运行前
        检查通过之后才记）。
        """

        reply = await service_client("账号句柄").call(
            RUNNER_ACCOUNT_OPEN,
            account_open_request(
                f"automas-script-maafw:{self.script_info.script_id}",
                self.cur_user_item.name,
                time.time(),
            ),
        )
        if reply.failed:
            raise RuntimeError(reply.message)
        self._account = str(reply["account"])

    async def _close_account(self) -> None:
        """关账号句柄（总是成功；还有作业时运行服务延后到最后一个作业结束）。"""

        account, self._account = self._account, None
        if account is None:
            return
        reply = await service_client("账号句柄").call(
            RUNNER_ACCOUNT_CLOSE, account_request(account)
        )
        if reply.failed:
            logger.warning(f"关闭 MFW 运行账号句柄没成功：{reply.message}")

    async def check(self) -> str:
        if self._account is None:
            await self._open_account()
        # 与管理器筛账号同一判据（proxy_quota）：全员到上限时管理器不做运行前更新
        if config_view.proxy_times_used_up(
            self.cur_user_config, self.script_config, self.curdate
        ):
            await self.cur_user_item.set_state(status="跳过")
            return "今日代理次数已达上限，跳过该账号"

        busy_reason = await self._try_enter_project_path()
        if busy_reason is not None:
            # 项目被占着没跑成：记「异常」而不是「跳过」
            await self.cur_user_item.set_state(status="异常")
            self.project_busy_skipped = True
            if busy_reason == "updating":
                return "项目正在更新，本次未运行该账号"
            return (
                "项目正被编辑页的准备运行环境、导入或另一次运行占用，本次未运行该账号"
            )

        keep_reservation = False
        try:
            loop = asyncio.get_running_loop()

            def send_plan_log(message: str) -> None:
                # 运行计划在工作线程里构建，排队列的账号日志要回到事件循环线程再写
                loop.call_soon_threadsafe(self._append_log, message)

            try:
                # 建计划（两段工作线程，中间在事件循环上查一次活动排期）可能提交配置；取消后先等这一整段
                # 做完，不能让它越过 X 锁释放。
                # 后半段线程里调运行服务的同步操作（desktop_check）：解析器在这里于事件循环上取好，线程里
                # 每次调用、每次重试都经它现取服务
                thread_client = self._thread_client()
                build_task = asyncio.create_task(
                    self._load_run_state(send_plan_log, thread_client)
                )
                try:
                    state = await asyncio.shield(build_task)
                except asyncio.CancelledError:
                    while not build_task.done():
                        try:
                            await asyncio.shield(build_task)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    with suppress(Exception):
                        build_task.result()
                    raise
                (
                    self.interface_model,
                    self.base_run_plan,
                    self.run_plan,
                    game_path_error,
                ) = state
                # 计划重建了，agent 命令计划下次借环境时按新的声明重算
                self._worker_agent_plans = None
            except Exception as exc:
                await self.cur_user_item.set_state(status="异常")
                return f"无法构建 MaaFW 运行计划，请检查项目和任务配置: {exc}"

            if not self.run_plan.tasks:
                await self.cur_user_item.set_state(status="跳过")
                return period_all_done_message(self.run_plan)

            # 本轮前面的用户已经撞上同一资源的停服维护：不再拉起游戏 / 模拟器
            maintenance = self.signal_tracker.maintenance(self.run_plan.resourceName)
            if maintenance is not None:
                maintenance.add_user(str(self.cur_user_uid), self.cur_user_item.name)
                await self.cur_user_item.set_state(status="跳过")
                self.maintenance_skipped = True
                return MAINTENANCE_SKIP_MESSAGE

            if self.run_plan.controllerType == "Adb":
                emulator_id = config_view.emulator_target(self.script_config)
                emulator_index = config_view.emulator_device(self.script_config)
                if emulator_id == "-" or emulator_index in ("", "-"):
                    await self.cur_user_item.set_state(status="异常")
                    return (
                        "当前 MaaFW controller 需要 ADB，请在托管管理页选择模拟器和实例"
                    )
            elif game_path_error is not None:
                await self.cur_user_item.set_state(status="异常")
                return game_path_error

            # 项目自带的 MaaFramework 与本机架构不符时 worker 里必然加载失败：先说清楚
            architecture_error = await asyncio.to_thread(
                self._describe_runtime_architecture_mismatch
            )
            if architecture_error:
                await self.cur_user_item.set_state(status="异常")
                return architecture_error

            keep_reservation = True
            return "Pass"
        finally:
            if not keep_reservation:
                await self._release_project_path()

    def _describe_runtime_architecture_mismatch(self) -> str | None:
        source = config_view.source_path(self.script_config)
        return describe_project_runtime_architecture_mismatch(
            self.project_path, source_path=Path(source) if source else None
        )

    async def prepare(self) -> None:
        start_time = datetime.now().astimezone()
        self.user_start_time = start_time
        self.cur_user_log_started_at = start_time
        self.cur_user_log = LogRecord()
        self.cur_user_log = await self.cur_user_item.add_log_record(
            start_time, self.cur_user_log
        )
        await self.cur_user_log.append_content(
            *self.project_update_logs, *self._pending_user_log
        )
        content = self.cur_user_log.content
        self._pending_user_log.clear()
        if content:
            await self.script_info.set_log("".join(content))

    # ------------------------------------------------------------------
    # 主流程与重试
    # ------------------------------------------------------------------

    async def main_task(self) -> None:
        self.curdate = proxy_today()
        self.check_result = await self.check()
        if self.check_result != "Pass":
            # 也记一行后端日志；「异常」按 WARNING，「跳过」按 INFO
            (logger.warning if self.cur_user_item.status == "异常" else logger.info)(
                f"MFW 用户运行前检查未通过（{self.cur_user_item.name}，{self.cur_user_item.status}）：{self.check_result}"
            )
            # 项目被占用不逐个弹窗
            if self.cur_user_item.status == "异常" and not self.project_busy_skipped:
                await Publisher.send(
                    id=self.task_info.task_id,
                    type=protocol.TASK_NOTICE,
                    data=WSTaskNoticeData(
                        level="error",
                        message=(
                            f"账号 {self.cur_user_item.name} 检查未通过: "
                            f"{self.check_result}"
                        ),
                    ),
                )
            if self.maintenance_skipped or self.project_busy_skipped:
                # 维护连带跳过、或项目被占用没跑成：记一份本账号的日志，让代理结果汇总与
                # 统计信息写明原因
                await self.prepare()
                self._append_log(self.check_result)
                if self.cur_user_log is not None:
                    await self.cur_user_log.set_state(status=self.check_result)
            else:
                # 没有本账号的日志：窗口只剩检查结果这一行
                await self.script_info.set_log(self.check_result)
            return

        await self._mark_run_started()
        await self.prepare()
        await self.cur_user_item.set_state(status="运行")
        # AttachOnly 兜底关闭的分界（之后才出现的游戏 / 启动器才算本次运行打开的）在开账号句柄时
        # 交给了运行服务（account_open 的 run_started_at）
        logger.info(f"开始代理 MaaFW 账号: {self.cur_user_uid}")
        if self.run_plan is not None:
            self._append_log(
                format_run_overview_log(
                    self.run_plan,
                    selected_preset=config_view.selected_preset(self.cur_user_config),
                )
            )
            for warning in self.run_plan.warnings:
                self._append_log(f"MaaFW 运行计划提示: {warning}")

        # 整个账号运行期间保持显示开启，屏已熄就先叫醒；收尾时撤销，final_task 再兜一次。
        self._ensure_display_hold()
        self._user_teardown_pending = True
        # 执行任务前脚本（每账号仅一次，重试不重复跑）。
        enabled, before_path = config_view.script_before_task(self.cur_user_config)
        if enabled:
            await execute_script_task(before_path, "脚本前任务")

        # 上一次尝试的失败摘要与是否重启过客户端：新一次另起日志后在开头写一句
        previous_failure: str | None = None
        previous_restarted = False
        # 运行前设置放进每次尝试里：它自己失败要能重试；客户端被关后重试也要靠它重新拉起
        pretasks_done = False
        # 上一次失败后 MAS 自己起的游戏 / 模拟器是否已关掉，这时运行前设置要重跑
        client_reset = False
        # 资源包加载失败后换了载荷、立刻再跑的那一次不计入尝试次数
        rerun_note: tuple[str, str] | None = None
        previous_label = ""
        index = -1
        while True:
            # 不用 range：资源包加载失败后清理过载荷时额外再跑一次（_extra_attempts），上限会变。
            index += 1
            if index >= self._attempt_limit() or self.run_complete:
                break
            begin_attempt(self, index + 1)

            # 每次尝试单独一份日志：第一次沿用 prepare() 建的那份，之后每次另起。
            if index > 0:
                await self._start_attempt_log()

            if rerun_note is not None:
                header, current_label = rerun_note
                self._append_log(f"账号 {self.cur_user_item.name} - {header}")
                rerun_note = None
                self._attempt_title = current_label
            else:
                ordinal = index + 1 - self._extra_attempts
                self._append_log(
                    f"账号 {self.cur_user_item.name} - 尝试次数: "
                    f"{ordinal}/{config_view.run_times_limit(self.script_config)}"
                )
                current_label = f"第 {ordinal} 次"
                self._attempt_title = f"{current_label}尝试"
            if index > 0 and previous_failure:
                self._append_log(
                    f"上一次（{previous_label}）：{previous_failure}"
                    + ("，已重启游戏/模拟器" if previous_restarted else "")
                )
            previous_label = current_label
            self._ledger.begin(
                id(self.cur_user_log),
                attempt=index + 1,
                title=self._attempt_title,
                plan=self.run_plan,
            )

            try:
                if index > 0:
                    self._log_retry_skipped_tasks()
                if self.run_plan is None or self.interface_model is None:
                    raise RuntimeError("MaaFW 运行计划尚未初始化")
                self._ensure_display_hold()
                # 放在运行前设置之前：锁屏时截图和输入用不了，进了重试还会白白重启游戏。
                await self._ensure_workstation_unlocked()
                if not pretasks_done or await self._pretasks_need_rerun(client_reset):
                    # 先清标记：重跑到一半失败时，下一轮不能因为窗口已被拉起就跳过
                    pretasks_done = False
                    with self._ledger.stage(
                        STAGE_PRETASK,
                        enabled=bool(getattr(self.run_plan, "pretasks", None)),
                    ):
                        await self._run_pretasks()
                    pretasks_done = True
                with self._ledger.stage(STAGE_CLIENT):
                    await self._ensure_desktop_game_started()
                    device_config = await self._build_device_config(
                        self.run_plan,
                        self.interface_model,
                    )
                result = await self._run_maafw(device_config)
            except Exception as exc:
                # 怎么走只读判定（run_verdict.judge_attempt），文案与通知照旧在这里发。
                verdict = judge_attempt(exc)
                message = str(verdict.message)
                self._append_log(message, warning=True)
                self._record_attempt(
                    index + 1,
                    [],
                    message,
                    user_message=verdict.user_message,
                    category=verdict.category,
                )
                if verdict.environment_unusable:
                    self._append_log(
                        "运行环境不可用，重试也不会有别的结果，已停止本轮",
                        warning=True,
                    )
                if self.cur_user_log is not None:
                    await self.cur_user_log.set_state(status=message)
                await Publisher.send(
                    id=self.task_info.task_id,
                    type=protocol.TASK_NOTICE,
                    data=WSTaskNoticeData(level="error", message=message),
                )
                if not verdict.retry:
                    break
                previous_failure = message
                if verdict.restart_client:
                    (
                        previous_restarted,
                        client_reset,
                    ) = await self._reset_client_for_retry(index + 1)
                continue

            await self._mark_period_tasks_completed(result.completedTasks)
            verdict = judge_attempt(result)
            if verdict.outcome == VERDICT_SIGNAL:
                # 项目声明的信号节点命中：不重启游戏 / 模拟器，直接收尾。
                point(
                    self,
                    "run",
                    state="skipped" if verdict.maintenance else "failed",
                )
                await self._finish_on_signal(index + 1, result)
                break
            succeeded = verdict.outcome == VERDICT_SUCCEEDED
            point(self, "run", state="succeeded" if succeeded else "failed")
            if succeeded:
                self.run_complete = True
                if self.cur_user_log is not None:
                    await self.cur_user_log.set_state(status="Success!")
                # 被项目 pipeline 自己停下的任务照旧算完成，只是另起一行写明被停止
                for line in completed_result_log_lines(self.run_plan, result):
                    self._append_log(line)
                self._record_attempt(
                    index + 1, result_task_labels(self.run_plan, result), None
                )
            else:
                if verdict.repair_resource and result.resourceFailure is not None:
                    # 资源包没能加载：原因只在原生 ERR 行里，换成一句人话
                    message = await asyncio.to_thread(
                        describe_resource_failure,
                        result.resourceFailure.kind,
                        result.resourceFailure.path,
                        self._attempt_native_errors,
                        project_root=self.project_path,
                        secrets=self._secret_log_variants(),
                    )
                else:
                    message = describe_failed_result(
                        result,
                        self.run_plan,
                        run_time_limit=config_view.run_time_limit(self.script_config),
                    )
                if self.cur_user_log is not None:
                    await self.cur_user_log.set_state(status=message)
                self._append_log(message, warning=True)
                self._record_attempt(
                    index + 1,
                    result_task_labels(self.run_plan, result),
                    message,
                    user_message=result.userMessage,
                    failed_task=result.failedTask,
                    screenshots=[
                        (
                            format_completed_task_labels(self.run_plan, [shot.task])[0],
                            Path(shot.path),
                        )
                        for shot in result.failureScreenshots
                    ],
                    category=verdict.category,
                )
                if verdict.repair_resource and result.resourceFailure is not None:
                    # 不重启游戏 / 模拟器；只有视图换过载荷时立刻再跑一次（不占重试次数）。
                    rerun_note = await self._recover_resource_failure(
                        result.resourceFailure.kind
                    )
                    if rerun_note is not None:
                        self._extra_attempts += 1
                        previous_failure = message
                        previous_restarted = False
                        client_reset = False
                        continue
                    break
                if not verdict.retry:
                    # worker 在任务里报的环境级失败：每轮都一样，不再重试。
                    if verdict.environment_unusable:
                        self._append_log(
                            "运行环境不可用，重试也不会有别的结果，已停止本轮",
                            warning=True,
                        )
                    break
                await self._refresh_plan(result.completedTasks)
                if self.run_plan is not None and not self.run_plan.tasks:
                    self.run_complete = True
                    # 计划被重试跳过剔空的，不能说成「周期任务」
                    self._append_log(remaining_done_message(self.run_plan))
                else:
                    previous_failure = message
                    if verdict.restart_client:
                        (
                            previous_restarted,
                            client_reset,
                        ) = await self._reset_client_for_retry(index + 1)

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------

    async def final_task(self) -> None:
        if self._user_finalized:
            return
        self._user_finalized = True
        self.history.begin_user_history_capture(
            self.script_info,
            self.cur_user_item,
            failed=self.check_result == "Pass"
            and not self.run_complete
            and self.run_signal != SERVER_MAINTENANCE
            and not self.stopped_manually,
        )
        # 收尾一开头专项自己写的账号日志（「已手动停止」）先压住，等运行服务在收尾里第一次发日志、或本次
        # 历史存完再写进日志记录。账号日志是异步写进记录的：原来收尾里在运行积木真去关游戏、停进程之前
        # 不让出事件循环，这一行要么随那一刻写进去、要么落在历史之后；收尾改调服务以后每次调用都会
        # 让出，不压住它就总在关游戏之前进历史
        self._held_publishes = []
        if self.stopped_manually:
            self._append_log("已手动停止", warning=True)
        # 收尾里抛的异常（例如脚本后任务失败）原样外抛，由宿主旧生命周期桥
        # （_LegacyExecution._finalize）调一次 on_crash；这里再调就会记两次异常、发两条通知。
        # 但历史在下面的 finally 里就存，等不到 on_crash：先把这次尝试记成异常再存。
        crashed = False
        try:
            await self._finalize_user()
        except Exception as error:
            crashed = True
            with suppress(Exception):
                await self._mark_user_crashed(error)
            raise
        finally:
            try:
                if not self._history_saved:
                    await self._save_user_history()
            finally:
                self._release_held_publishes()
                try:
                    if self.stopped_manually:
                        # 即使设备关闭或统计中断，也必须把预约交给安全释放检查。
                        await self._release_project_path()
                finally:
                    try:
                        await asyncio.to_thread(self._history_temp.cleanup)
                    finally:
                        # 运行服务的账号句柄在最外层关（总是成功；退出确认等作业还在时延后关）。
                        # 收尾抛出时生命周期桥接着调 on_crash（停运行前设置、关游戏还要用账号），
                        # 留到 on_crash 最后关
                        if crashed:
                            self._close_account_after_crash = True
                        else:
                            await self._close_account()

    async def _finalize_user(self) -> None:
        if self._user_teardown_pending:
            self._user_teardown_pending = False
            # 后脚本可能仍需使用模拟器，必须先完成，再关闭资源与保存历史。
            enabled, after_path = config_view.script_after_task(self.cur_user_config)
            if enabled:
                await execute_script_task(after_path, "脚本后任务")
        self._release_display_hold()
        await self._shutdown_runner()
        if self.check_result != "Pass":
            if self.maintenance_skipped:
                # 维护连带跳过：同样是跳过不是失败，代理次数不动
                await config_view.set_last_proxy_status(
                    self.cur_user_config, MAINTENANCE_LAST_STATUS
                )
                await self._save_user_history()
                await self._push_user_statistics()
            elif self.project_busy_skipped:
                # 没跑成：统计信息写明原因；代理次数与上次状态不动。
                await self._save_user_history()
                await self._push_user_statistics()
            await self._release_project_path()
            return

        await self._close_emulator()
        await self._close_game_after_run(failed=not self.run_complete)
        if self.run_complete:
            # 当天第一次成功才扣剩余天数（-1 不限不扣），代理次数加 1；逐条写、逐条提交
            remained_day, proxy_times = success_settlement(
                config_view.proxy_times(self.cur_user_config),
                config_view.remained_day(self.cur_user_config),
            )
            if remained_day is not None:
                await config_view.set_remained_day(self.cur_user_config, remained_day)
            await config_view.set_proxy_times(self.cur_user_config, proxy_times)
            await config_view.set_last_proxy_status(self.cur_user_config, "成功")
            await self.cur_user_item.set_state(status="完成")
        elif self.run_signal == SERVER_MAINTENANCE:
            # 维护是跳过不是失败：代理次数、剩余天数不动
            await config_view.set_last_proxy_status(
                self.cur_user_config, MAINTENANCE_LAST_STATUS
            )
            await self.cur_user_item.set_state(status="跳过")
        else:
            await config_view.set_last_proxy_status(self.cur_user_config, "失败")
            if self.cur_user_item.status == "运行":
                await self.cur_user_item.set_state(status="异常")

        await self._save_user_history()
        if self.run_complete:
            await self._send_success_notify()
        await self._push_user_statistics()
        await self._release_project_path()

    async def _mark_user_crashed(self, e: Exception) -> None:
        """把这次尝试记成崩溃：账号日志状态写异常原文，history 状态记 error。

        同一个异常只记一次：收尾抛的异常 ``final_task`` 存历史前先记，生命周期桥随后调
        ``on_crash`` 时不再重写日志状态。
        """

        if getattr(self, "_crash_marked", None) is e:
            return
        # 还没进第一次尝试就崩了（prepare、脚本前任务出错）时推送靠它说原因
        self._crash_message = str(e)
        if self.cur_user_log is not None:
            await self.cur_user_log.set_state(status=f"MaaFW 内置运行任务出现异常: {e}")
            if self.cur_user_log_started_at is not None:
                self._ledger.mark_error(id(self.cur_user_log))
        self._crash_marked = e

    async def on_crash(self, e: Exception) -> None:
        await self.cur_user_item.set_state(status="异常")
        logger.exception(f"MaaFW 内置运行任务出现异常: {e}")
        await self._mark_user_crashed(e)
        try:
            await Publisher.send(
                id=self.task_info.task_id,
                type=protocol.TASK_NOTICE,
                data=WSTaskNoticeData(
                    level="error",
                    message=f"MaaFW 内置运行任务出现异常: {e}",
                ),
            )
        except Exception:
            pass
        try:
            await self._shutdown_runner()
            await self._close_emulator()
            await self._close_game_after_run(failed=True)
            await self._release_project_path()
        finally:
            if self._close_account_after_crash:
                self._close_account_after_crash = False
                await self._close_account()

    # ------------------------------------------------------------------
    # 建计划（工作线程）
    # ------------------------------------------------------------------

    @staticmethod
    def _thread_client() -> ServiceClient:
        """给工作线程用的客户端（``for_thread``：解析器在事件循环上取好）；槽没绑时退回经槽取的那个，
        线程里调用时回 ``service_unavailable``。"""

        client = service_client("运行前检查")
        try:
            return client.for_thread()
        except ServiceUnavailable:
            return client

    async def _load_run_state(
        self,
        send_log: Callable[[str], None] | None = None,
        thread_client: ServiceClient | None = None,
    ) -> tuple[MaaFWInterface, MaaFWRunPlan, MaaFWRunPlan, str | None]:
        """建这一轮的计划：工作线程（前半段）→ 事件循环（按需查一次活动排期）→ 工作线程（后半段）。

        ``check`` 用原来那个 shield 循环等这整个协程：用户这时点停止，照样等它做完。``thread_client``
        是给后半段工作线程调运行服务同步操作（``desktop_check``）用的客户端（:meth:`_thread_client`）。
        """

        head = await asyncio.to_thread(self._plan_head, send_log)
        running: bool | None = None
        # 只在前半段说要查时查（活动不在队列里要补 / 在队列里要判，两者互斥），每次运行最多一次
        if head.state.probe_needed:
            # 按需导入、按模块属性调：本包在导入期不拉起网络模块（经 SDK 出口取宿主的星塔日历）
            from auto_mas_core.host.tools_common_game_calendar import stella

            try:
                running = await stella.has_running_official_activity()
            except Exception as exc:
                raise MaaFWRunPlanError(
                    describe_plan_failure(str(exc), head.inspection, head.expansion)
                ) from exc
        if thread_client is None:
            thread_client = self._thread_client()
        return await asyncio.to_thread(
            self._plan_tail, head, running, send_log, thread_client
        )

    def _plan_head(self, send_log: Callable[[str], None] | None) -> "_PlanHead":
        """前半段（工作线程）：读 interface、展开任务快照、套计划表，算出要不要查活动排期。"""

        interface_model = MaaFWInterfaceService().load(self.project_path)
        # 账号页上配的任务队列就是唯一的任务来源（不看 Info.IfQuickConfig）。
        task_snapshot = load_json_dict(
            config_view.task_snapshot_raw(self.cur_user_config)
        )
        selected_preset = config_view.selected_preset(self.cur_user_config)
        controller_name, resource_name = config_view.run_selection(
            self.script_config, interface_model
        )
        effective_preset = (
            selected_preset if selected_preset and not task_snapshot else None
        )
        # 托管级键位（Game.Hotkeys）：坏 JSON / 不是对象当空，字段级的校验在建计划时做。
        script_hotkeys = load_script_hotkeys(
            config_view.hotkeys_raw(self.script_config)
        )
        # 本方法在工作线程里跑：没给线程安全的回调就只进后端日志。
        log = send_log if send_log is not None else logger.info
        inspection: MaaFWSnapshotInspection | None = None
        expansion: MaaFWSnapshotExpansion | None = None
        try:
            # 密码字段在配置里是密文，只在这份内存副本里解开交给计划。
            task_snapshot = open_task_snapshot(task_snapshot, interface_model)
            # 运行日按本机今天的星期判，只在开跑建计划时判这一次。
            iso_weekday = datetime.now().isoweekday()
            inspection = inspect_task_snapshot(
                self.project_path,
                interface_model,
                task_snapshot,
                iso_weekday=iso_weekday,
            )
            self.missing_task_names = list(dict.fromkeys(inspection.missing_task_names))
            # 先展开任务快照（剔掉今天不在运行日的实例；MSS 没有规则表），再按 MSS 的规矩排队列。
            expansion = expand_task_snapshot(
                self.project_path,
                interface_model,
                task_snapshot=task_snapshot,
                selected_preset=effective_preset,
                iso_weekday=iso_weekday,
                rules=EMPTY_PLAN_RULES,
                inspection=inspection,
            )
            for line in expansion.notices:
                log(line)
            # 日常：计划表今天那一格改写悬赏试炼。红灯与今天不跑的实例不自动补回（按任务名比）。
            plan_key = config_view.plan_slot(self.cur_user_config, log)
            state = mss_arrange_head(
                interface_model,
                list(expansion.task_ids),
                dict(expansion.task_options),
                excluded_ids=expansion.excluded_task_ids,
                plan_key=plan_key,
                if_activity_first=config_view.if_activity_first_raw(
                    self.cur_user_config
                ),
                log=log,
            )
        except Exception as exc:
            raise MaaFWRunPlanError(
                describe_plan_failure(str(exc), inspection, expansion)
            ) from exc
        return _PlanHead(
            interface_model=interface_model,
            task_snapshot=task_snapshot,
            controller_name=controller_name,
            resource_name=resource_name,
            script_hotkeys=script_hotkeys,
            inspection=inspection,
            expansion=expansion,
            state=state,
        )

    def _plan_tail(
        self,
        head: "_PlanHead",
        running: bool | None,
        send_log: Callable[[str], None] | None,
        thread_client: ServiceClient,
    ) -> tuple[MaaFWInterface, MaaFWRunPlan, MaaFWRunPlan, str | None]:
        """后半段（工作线程）：按活动排期处理活动任务、爬塔归位，按排好的队列建计划，再做周期过滤
        与桌面检查（运行服务的同步操作 ``desktop_check``，经 ``thread_client`` 现取）。"""

        interface_model = head.interface_model
        log = send_log if send_log is not None else logger.info
        try:
            arranged_ids, arranged_options = mss_arrange_tail(
                head.state, running, interface_model=interface_model, log=log
            )
            # 其余停跑日实例在建计划前排除、记成跳过（不校验选项），全被排除时计划为空。
            base_run_plan = build_arranged_plan(
                self.project_path,
                interface_model,
                expansion=head.expansion,
                inspection=head.inspection,
                task_ids=arranged_ids,
                task_options=arranged_options,
                task_snapshot=head.task_snapshot,
                controller_name=head.controller_name,
                resource_name=head.resource_name,
                script_hotkeys=head.script_hotkeys,
                rules=EMPTY_PLAN_RULES,
                client_version=config_view.client_version(),
            )
        except Exception as exc:
            raise MaaFWRunPlanError(
                describe_plan_failure(str(exc), head.inspection, head.expansion)
            ) from exc
        run_plan = self._filter_period_once_tasks(base_run_plan)

        game_path_error: str | None = None
        if (
            _desktop_process_target().get() is not None
            and run_plan.controllerType != "Win32"
        ):
            raise ValueError("MaaFW 桌面分身仅支持 Win32 控制器")
        if (
            run_plan.tasks
            and run_plan.controllerType == "Win32"
            and (not optional_int(config_view.configured_hwnd(self.script_config)))
            and (
                not controller_has_window_rules(
                    find_controller(interface_model, run_plan.controllerName)
                )
            )
        ):
            # 控制器一条窗口匹配规则都没写，又没指定句柄：在拉起游戏之前就报。
            game_path_error = WIN32_NO_WINDOW_RULES_MESSAGE
        elif run_plan.tasks and run_plan.controllerType == "Win32":
            # MAS 管不管启动游戏、管的话缺什么配置（运行服务的同步操作 desktop_check，读盘）
            checked = thread_client.call_sync(
                RUNNER_DESKTOP_CHECK,
                desktop_check_request(config_view.desktop_spec(self.script_config)),
            )
            if checked.failed:
                raise RuntimeError(checked.message)
            if checked.get("managed"):
                game_path_error = checked.get("error")
        return interface_model, base_run_plan, run_plan, game_path_error

    # ------------------------------------------------------------------
    # 设备配置
    # ------------------------------------------------------------------

    async def _build_device_config(
        self,
        plan: MaaFWRunPlan,
        interface_model: MaaFWInterface,
    ) -> dict[str, Any]:
        """取配置原值与模拟器入口，交给运行服务的 ``device_*`` 操作换算成设备配置（字典）。

        MSS 没有运行前登录与游戏更新：ADB（引擎不拦）不插任何步骤、不问 Toolkit，Win32 直接找窗口。
        操作回的日志按发生先后写进账号日志；失败按 §2.2 抛同一句话。
        """
        runner = service_client("设备配置")
        if plan.controllerType == "Adb":
            state = self._adb_state
            address, device_info = await self._resolve_adb_address()
            emulator_id = str(config_view.emulator_target(self.script_config) or "")
            device_uid = config_view.emulator_device(self.script_config)
            # MSS 没有运行前登录：宿主没定下 adb 路径时也不去问 Toolkit
            paths = self._dispatched(
                await runner.call(
                    RUNNER_DEVICE_ADB_PATH,
                    device_adb_path_request(
                        emulator_id=emulator_id,
                        device_uid=device_uid,
                        configured_path=config_view.adb_path_raw(self.script_config),
                        device_title=getattr(device_info, "title", None),
                        address=address,
                        host_adb_path=state.adb_path,
                        ask_toolkit="never",
                    ),
                )
            )
            state.adb_path = paths.get("host_adb_path")
            screencap_methods, input_methods = config_view.adb_methods_raw(
                self.script_config
            )
            configured = self._dispatched(
                await runner.call(
                    RUNNER_DEVICE_ADB_CONFIG,
                    device_adb_config_request(
                        emulator_id=emulator_id,
                        target_id=emulator_id,
                        device_uid=device_uid,
                        address=address,
                        adb_path=paths.get("adb_path"),
                        profile=state.profile,
                        process_id=getattr(device_info, "process_id", None),
                        process_started_at=getattr(
                            device_info, "process_started_at", None
                        ),
                        configured_screencap_methods=screencap_methods,
                        configured_input_methods=input_methods,
                    ),
                )
            )
            state.profile = dict(configured["profile"])
            return dict(configured["device_config"])

        if plan.controllerType == "Win32":
            controller = find_controller(interface_model, plan.controllerName)
            screencap, mouse, keyboard = config_view.win32_methods_raw(
                self.script_config
            )
            configured = self._dispatched(
                await runner.call(
                    RUNNER_DEVICE_WIN32_CONFIG,
                    device_win32_config_request(
                        controller=controller,
                        configured_hwnd=config_view.configured_hwnd(
                            self.script_config
                        ),
                        configured_screencap_method=screencap,
                        configured_mouse_method=mouse,
                        configured_keyboard_method=keyboard,
                    ),
                )
            )
            return dict(configured["device_config"])

        raise RuntimeError(f"当前仅支持 Adb/Win32 controller: {plan.controllerType}")

    def _dispatched(self, reply: Reply) -> Reply:
        """短操作回的 ``events``（失败时在 ``detail`` 里）按先后写进账号日志；失败按 §2.2 抛同一句话
        （``ServiceFailure``，``str()`` 是原文）。"""

        events = reply.get("events") if reply.ok else reply.detail.get("events")
        for event in events or ():
            if isinstance(event, Mapping):
                self._on_service_event(event)
        return reply.expect_ok()

    async def _resolve_adb_address(self) -> tuple[str, DeviceInfo | None]:
        state = self._adb_state
        if state.address is not None:
            return state.address, state.device_info
        if self.emulator_manager is None:
            raise RuntimeError("当前 controller 需要 ADB，请在托管管理页选择模拟器")

        emulator_index = config_view.emulator_device(self.script_config)
        if emulator_index in ("", "-"):
            raise RuntimeError("当前 controller 需要 ADB，请在托管管理页选择模拟器实例")

        package_name = await choose_game_package(
            config_view.game_package_name(self.script_config),
            self.run_plan,
            log=self._append_log,
        )
        self.opened_emulator = True
        # 开模拟器、冷启动游戏时打 launched 点与记时刻、静默时藏窗口（host_client）
        launch = await open_emulator_for_run(
            self.emulator_manager,
            emulator_index,
            package_name,
            silent=config_view.silent_mode(),
            emit=self._on_service_event,
        )
        device_info = launch.device_info
        # 冷启动游戏时首任务闸门改按读盘判据放行（task_start_gate）
        self._emulator_started_at = launch.started_at
        self._disk_quiet = launch.started_at is not None
        if not device_info.adb_address or device_info.adb_address == "Unknown":
            raise RuntimeError("模拟器未返回可用 ADB 地址")

        state.address = device_info.adb_address
        state.device_info = device_info
        self._append_log(f"模拟器启动完成，ADB 地址: {device_info.adb_address}")
        return device_info.adb_address, device_info

    # ------------------------------------------------------------------
    # 跑计划
    # ------------------------------------------------------------------

    async def _run_maafw(self, device_config: Mapping[str, Any]) -> MaaFWRunResult:
        """取配置、组装入参，交给运行服务的 ``plan_*`` 跑这份计划；事件转成账号日志、运行记录与进度点。

        失败（``run_timeout``、``task_deadline_expired``、``run_failed``、提供方没了）抛
        ``ServiceFailure``（``str()`` 是原文，判定按 ``code``）；被停时按 null 族的规则等作业收完再抛取消。
        附件、原生报错行与 agent 命令计划在成功、失败、被停时都从结果里取。
        """

        plan = self.run_plan
        if plan is None:
            raise RuntimeError("MaaFW 运行计划尚未初始化")
        task_limit_raw, task_overrides_raw = config_view.task_time_limit_raw(
            self.script_config
        )
        task_limit_seconds, task_limit_overrides = task_time_limits(
            task_limit_raw, task_overrides_raw
        )
        # MSS 没有交给首任务的登录，不给任何任务截止时刻
        task_deadlines: dict[str, float] = {}
        # 与运行前更新 / 预检同一份解析（项目服务 project_context）：托管级 Update.ProxyAddress 优先，
        # 留空跟随全局
        proxy_url = (await self._project_context()).get("proxy_url")
        secrets = self._secret_log_variants()
        profile = self._adb_state.profile
        project_log_secrets = project_log_secret_variants(
            secrets,
            config_view.all_task_snapshots(self.script_config),
            self.interface_model,
            DPAPI_SECRET_OPENER,
        )
        redaction_notice = self._log_redaction_notice()
        extra_env = await self._script_adb_env()
        win32 = plan.controllerType == "Win32"
        request = plan_request(
            account=self._require_account(),
            project_path=self.project_path,
            plan=plan,
            device_config=device_config,
            runtime_pool_root=self.maafw_runtime_pool_root,
            runtime_pool_id=str(self.maafw_runtime_pool_id or ""),
            lease_owner=f"automas-script-maafw:{self.script_info.script_id}",
            run_time_limit_minutes=config_view.run_time_limit(self.script_config),
            attachment_root=Path(self._history_temp.name),
            task_time_limit_seconds=task_limit_seconds,
            task_time_limit_overrides=task_limit_overrides,
            task_deadlines=task_deadlines,
            loop_guard=loop_guard(config_view.loop_guard_raw(self.script_config)),
            run_secrets=secrets,
            project_log_secrets=project_log_secrets,
            redaction_notice=redaction_notice,
            # 同一账号运行里只走一条路：Win32 是桌面游戏刚启动的时刻（运行服务账号里记着），
            # ADB 是随模拟器冷启动的时刻
            game_start_source="desktop" if win32 else "given",
            game_started_at=None if win32 else self._emulator_started_at,
            game_start_disk_quiet=self._disk_quiet,
            game_wait_seconds=config_view.game_wait_time(self.script_config),
            proxy_url=proxy_url,
            agent_plans=self._worker_agent_plans,
            extra_env=extra_env,
            # 桌面分身目标由运行服务在这次调用里读（current ContextVar）；ModAVD 给模拟器 id
            avd_emulator_id=(
                str(config_view.emulator_target(self.script_config) or "")
                if profile is not None and profile.get("emulator_type") == "avd"
                else None
            ),
        )
        attempt_key = id(self.cur_user_log)

        def take_extras(result: Mapping[str, Any]) -> None:
            # 附件、原生报错行与 agent 命令计划在运行失败、被停时同样要留下（失败时在 detail 里）
            extras = result if result.get("ok") else result.get("detail") or {}
            attachments = extras.get("attachments") or []
            if attachments:
                self._ledger.add_attachments(attempt_key, attachments)
            self._attempt_native_errors = list(extras.get("native_errors") or [])
            # 同一账号这一轮只认第一次借环境时算出的 agent 命令计划，重试沿用
            plans = extras.get("agent_plans")
            if self._worker_agent_plans is None and plans is not None:
                self._worker_agent_plans = list(plans)

        self._attempt_native_errors = []
        operation = service_client("跑计划").operation(
            RUNNER_PLAN, on_event=self._on_service_event, on_result=take_extras
        )
        self._plan_op = operation
        try:
            reply = await operation.run(request)
        finally:
            if self._plan_op is operation:
                self._plan_op = None
        if reply.failed:
            raise reply.failure()
        return MaaFWRunResult.model_validate(reply["result"])

    async def _script_adb_env(self) -> dict[str, str]:
        """脚本专用 adb server 的进程环境（``host_client.script_adb_env``）。"""

        return await script_adb_env(
            self.emulator_manager, config_view.emulator_device(self.script_config)
        )

    def _on_service_log(
        self, message: str, *, warning: bool = False, at: float | None = None
    ) -> None:
        # 收尾里压住的那几行（见 final_task）：运行服务开始在收尾里写日志了，原来这时早已让出过
        # 事件循环、它们已经写进去了，先放出来再写这一行（先后不变）
        self._release_held_publishes()
        self._append_log(message, warning=warning, at=at)

    def _on_service_point(self, stage: Any, *, state: str, key: str) -> None:
        point(self, stage, state=state, key=key)

    def _on_service_stage(self, stage: Any, ok: bool | None) -> None:
        self._ledger.mark_stage(stage, ok)

    def _require_account(self) -> str:
        """运行服务的账号句柄（运行前检查一开头就开了）；没开成时按提供方没了说那句固定话。"""

        if self._account is None:
            raise RuntimeError(unavailable_message(RUNNER))
        return self._account

    def _on_run_record_event(self, data: Any) -> None:
        """将 worker 推来的阶段、任务起止事件归入本次尝试的专项记录。"""

        event = self._ledger.apply_record_event(data)
        task_point = record_event_point(event)
        if task_point is not None:
            name, state = task_point
            point(self, "special", key=name, state=state)
        if self._ledger.current_has_success():
            from auto_mas_core.host.core_script_update_queue import report_script_task_completed

            report_script_task_completed()

    # ------------------------------------------------------------------
    # 打码值（每个调用点现算）
    # ------------------------------------------------------------------

    def _run_password_values(self) -> list[str]:
        return run_secret_values(
            self.run_plan,
            self.interface_model,
            config_view.task_snapshot_secret_raw(
                getattr(self, "cur_user_config", None)
            ),
            DPAPI_SECRET_OPENER,
        )

    def _secret_log_variants(self) -> list[str]:
        return secret_log_variants(self._run_password_values())

    def _log_redaction_notice(self) -> str | None:
        if self.run_plan is None or self.interface_model is None:
            return None
        return log_redaction_notice(self.interface_model, self._run_password_values())

    # ------------------------------------------------------------------
    # 运行前设置、worker 的停止
    # ------------------------------------------------------------------

    async def _shutdown_runner(self) -> None:
        if self._account is not None:
            # 运行前设置的清理（运行服务 pretask_kill，没有取消点：被取消时脱手、提供方照样做完）
            reply = await service_client("运行前设置").run(
                RUNNER_PRETASK_KILL,
                account_request(self._account),
                on_event=self._on_service_event,
            )
            if reply.failed:
                logger.warning(f"MFW 运行前设置清理没完成：{reply.message}")
        # 停止时结束正在跑的 worker（运行服务 plan_stop；没有在跑的就什么都不做）
        operation = self._plan_op
        if operation is not None:
            operation.stop()

    @staticmethod
    def _interface_controller(
        interface: MaaFWInterface | None, name: str | None
    ) -> Any:
        """interface 里叫 ``name`` 的控制器；没加载或找不到时 None（运行服务在原来那一步报找不到）。"""

        if interface is None or not name:
            return None
        return next((item for item in interface.controller if item.name == name), None)

    def _controller_fields(self) -> dict[str, Any]:
        """桌面游戏与重跑判定要的控制器：选中的那个（模型）、名字、interface 加载没有。"""

        plan = self.run_plan
        name = plan.controllerName if plan is not None else None
        return {
            "controller": self._interface_controller(self.interface_model, name),
            "controller_name": name,
            "interface_loaded": self.interface_model is not None,
        }

    async def _pretasks_need_rerun(self, client_reset: bool) -> bool:
        """重试前要不要再跑一遍已经成功过的运行前设置（运行服务 ``pretask_rerun_check``）。"""

        plan = self.run_plan
        fields = self._controller_fields()
        reply = await service_client("运行前设置").call(
            RUNNER_PRETASK_RERUN_CHECK,
            pretask_rerun_check_request(
                has_pretasks=bool(plan.pretasks) if plan is not None else False,
                client_reset=client_reset,
                controller_type=plan.controllerType if plan is not None else None,
                interface_loaded=fields["interface_loaded"],
                controller_name=fields["controller_name"],
                controller=fields["controller"],
                configured_hwnd=config_view.configured_hwnd(self.script_config),
            ),
        )
        return bool(self._dispatched(reply).get("rerun"))

    async def _run_pretasks(self) -> None:
        """取配置、组装入参，交给运行服务的 ``pretask_*`` 跑这次尝试的运行前设置。"""

        if self.run_plan is None:
            raise RuntimeError("MaaFW 运行计划尚未初始化")
        # ModAVD 实例：pretask 若用 adb，也走脚本专用 server（同 worker）
        adb_env = await self._script_adb_env()
        reply = await service_client("运行前设置").run(
            RUNNER_PRETASK,
            pretask_request(
                account=self._require_account(),
                project_path=self.run_plan.path,
                pretasks=self.run_plan.pretasks,
                run_time_limit_minutes=config_view.run_time_limit(self.script_config),
                plan_env=self.run_plan.piEnv,
                extra_env=adb_env,
                secrets=self._secret_log_variants(),
            ),
            on_event=self._on_service_event,
        )
        if reply.failed:
            # pretask_failed（判定按它不加前缀、记运行前设置失败）、run_failed、提供方没了
            raise reply.failure()

    # ------------------------------------------------------------------
    # 周期任务与重试时重建计划
    # ------------------------------------------------------------------

    def _filter_period_once_tasks(self, plan: MaaFWRunPlan) -> MaaFWRunPlan:
        limits = config_view.period_limits(self.script_config)
        if limits.empty:
            return plan
        return filter_period_once_tasks(
            plan,
            limits,
            self._load_period_task_records(),
            datetime.now(tz=UTC4),
        )

    async def _refresh_plan(self, completed_tasks: list[str]) -> None:
        """重试前重建计划：从 ``base_run_plan`` 重新按周期过滤，再剔掉本次运行已完成的。

        剔除必须跟在重建之后：重建会把上一次剔掉的任务放回来，所以按累计结果每次都重剔。
        同名任务在队列里有多份时全部重跑（见 ``skip_tasks_completed_this_run``）。
        """

        self._completed_this_run.extend(completed_tasks)
        if self.base_run_plan is not None:
            plan = await asyncio.to_thread(
                self._filter_period_once_tasks,
                self.base_run_plan,
            )
            outcome = skip_tasks_completed_this_run(
                plan, self._completed_this_run, MSS_SKIP_ON_RETRY
            )
            self._retry_rerun_duplicates = outcome.rerun_duplicates
            self.run_plan = outcome.plan

    def _log_retry_skipped_tasks(self) -> None:
        """重试开头写一行被重试跳过的任务；不一次性消费（worker 崩溃时计划原样沿用）。"""

        for line in retry_skip_log_lines(self.run_plan, self._retry_rerun_duplicates):
            self._append_log(line)

    def _load_period_task_records(self) -> PeriodRecords:
        return parse_period_records(
            load_json_dict(config_view.period_records_raw(self.cur_user_config))
        )

    async def _mark_period_tasks_completed(self, completed_tasks: list[str]) -> None:
        if not completed_tasks:
            return
        limits = config_view.period_limits(self.script_config)
        if limits.empty:
            return

        new_records = period_records_after(
            load_json_dict(config_view.period_records_raw(self.cur_user_config)),
            limits,
            completed_tasks,
            datetime.now(tz=UTC4),
        )
        if new_records is not None:
            await config_view.set_period_records(self.cur_user_config, new_records)

    async def _mark_run_started(self) -> None:
        if proxy_day_rolled(
            config_view.last_proxy_date(self.cur_user_config), self.curdate
        ):
            await config_view.set_last_proxy_date(
                self.cur_user_config, datetime.now(tz=UTC4)
            )
            await config_view.set_proxy_times(self.cur_user_config, 0)
        await config_view.set_last_proxy_status(self.cur_user_config, "运行中")

    # ------------------------------------------------------------------
    # 客户端（模拟器 / 桌面游戏）
    # ------------------------------------------------------------------

    async def _close_emulator(self) -> bool:
        """关闭由 MAS 启动的模拟器，返回是否关成（本来就没开过算关成）。"""

        if not self.opened_emulator:
            return True
        try:
            return await close_emulator(self)
        finally:
            self.opened_emulator = False
            # 地址与雷电 extras 里的 pid 都是随这次启动的，下一轮重开要重新拿
            self._adb_state.forget_emulator_launch()
            # 游戏随模拟器一起关了；下一轮重开时按那次拉起重新起算
            self._emulator_started_at = None
            self._disk_quiet = False

    async def _ensure_desktop_game_started(self) -> None:
        """Win32 场景下由 MAS 负责启动/激活桌面游戏客户端（运行服务 ``desktop_launch_*``）。桌面分身
        目标由运行服务在这次调用里读。"""
        if self.run_plan is None or self.run_plan.controllerType != "Win32":
            return
        fields = self._controller_fields()
        request = desktop_launch_request(
            self._require_account(),
            config_view.desktop_spec(self.script_config),
            fields["controller"],
        )
        request["controller_name"] = fields["controller_name"]
        request["interface_loaded"] = fields["interface_loaded"]
        reply = await service_client("桌面游戏").run(
            RUNNER_DESKTOP_LAUNCH,
            request,
            on_event=self._on_service_event,
            on_result=self._take_desktop_state,
        )
        if reply.failed:
            raise reply.failure()

    def _take_desktop_state(self, result: Mapping[str, Any]) -> None:
        """桌面游戏作业终态里的 ``desktop``（成功在顶层、失败在 ``detail``）：开没开过。"""

        desktop = result.get("desktop")
        if not isinstance(desktop, Mapping):
            detail = result.get("detail")
            desktop = detail.get("desktop") if isinstance(detail, Mapping) else None
        if isinstance(desktop, Mapping):
            self._desktop_opened = bool(desktop.get("opened"))

    def _attempt_limit(self) -> int:
        """本账号这次运行最多跑几次：``Run.RunTimesLimit``，加上清理残留后额外的重跑。"""

        return (
            int(config_view.run_times_limit(self.script_config) or 0)
            + self._extra_attempts
        )

    async def _recover_resource_failure(self, kind: str) -> tuple[str, str] | None:
        """资源包加载失败之后与发行包核对：视图换了载荷就返回下一次的（开头那行, 称呼）、该立刻再跑一次；
        其余情况它写一行停止原因、返回 None。"""

        # 项目服务的 repair 作业：跟随来源、渠道、代理、外壳提示按它的口径从托管记录算；每次运行只
        # 核对一次（already_checked）。用户停止时作业不收（取消规则 4）：交出来，交回项目预约时等它
        operation = service_client("资源包修复").operation(
            PROJECT_REPAIR, on_event=self._on_service_event
        )
        try:
            reply = await operation.run(
                repair_request(
                    script_id=str(self.script_info.script_id),
                    script_name=str(self.script_info.name or ""),
                    record=record_payload(
                        str(self.script_info.script_id), self.script_config
                    ),
                    kind=kind,
                    already_checked=self._repair_checked,
                )
            )
        except asyncio.CancelledError:
            if operation.handle is not None:
                self._repair_op = operation
            raise
        if reply.failed:
            raise reply.failure()
        if reply.get("checked_now"):
            self._repair_checked = True
        rerun = reply.get("rerun")
        if isinstance(rerun, Mapping):
            return str(rerun["line"]), str(rerun["title"])
        return None

    async def _reset_client_for_retry(self, attempt: int) -> tuple[bool, bool]:
        """重试前关掉 MAS 自己起的客户端，返回（是否全部关成, 是否已不在）。"""

        had_client = self._desktop_opened or self.opened_emulator
        restarted = await self._restart_client_before_retry(attempt)
        still_open = self._desktop_opened or self.opened_emulator
        return restarted, bool(had_client and not still_open)

    async def _restart_client_before_retry(self, attempt: int) -> bool:
        """一轮失败后把 MAS 自己拉起的游戏/模拟器关掉，让下一轮从启动重新来。

        游戏/模拟器不是 MAS 起的时照旧不碰；这是最后一轮时也不在这里关，收尾统一关。返回这次是否
        全部关成。
        """

        if attempt >= self._attempt_limit():
            return False
        if not self._desktop_opened and not self.opened_emulator:
            return False
        # 只在真正要重启 MAS 自己启动的客户端时撤销预下载资格。
        from auto_mas_core.host.core_script_update_queue import report_script_task_retrying

        await report_script_task_retrying()
        self._append_log("本轮失败，关闭由 MAS 启动的游戏/模拟器，下一轮重新启动")
        emulator_closed = await self._close_emulator()
        game_closed = await self._close_game()
        return emulator_closed and game_closed

    async def _quit_game(self, request: dict[str, Any]) -> Reply:
        """运行服务的 ``desktop_quit_*``（没有取消点：被取消时脱手、提供方照样关完、恢复分辨率）。

        失败（读不出客户端路径的原文、关游戏时出错）按 §2.2 抛同一句话；开没开过照结果更新。
        """

        reply = await service_client("桌面游戏").run(
            RUNNER_DESKTOP_QUIT,
            request,
            on_event=self._on_service_event,
            on_result=self._take_desktop_state,
        )
        if reply.failed:
            raise RuntimeError(reply.message)
        return reply

    async def _close_game(self) -> bool:
        """关闭由 MAS 启动的游戏，返回是否关成（本来就没开过算关成）；运行服务 ``desktop_quit``
        （``retry``）。"""

        if self._account is None:
            return True
        reply = await self._quit_game(
            desktop_quit_request(
                self._account,
                config_view.desktop_spec(self.script_config, for_close=True),
                mode="retry",
            )
        )
        return bool(reply.get("closed"))

    async def _close_game_after_run(self, *, failed: bool) -> None:
        """最终收尾关游戏：MAS 启动的照常关，AttachOnly 再按 ``Game.CloseMode`` 兜底（运行服务
        ``desktop_quit``，``final``）。桌面分身目标由运行服务在这次调用里读。"""

        if self._account is None:
            return
        plan = self.run_plan
        fields = self._controller_fields()
        request = desktop_quit_request(
            self._account,
            config_view.desktop_spec(self.script_config, for_close=True),
            mode="final",
            controller=fields["controller"],
            controller_type=plan.controllerType if plan is not None else None,
            failed=failed,
        )
        request["controller_name"] = fields["controller_name"]
        request["interface_loaded"] = fields["interface_loaded"]
        await self._quit_game(request)

    # ------------------------------------------------------------------
    # 项目预约、显示保持、锁屏
    # ------------------------------------------------------------------

    async def _try_enter_project_path(self) -> str | None:
        """拿本账号的项目预约（项目服务 ``run_reserve``）。拿到返回 None；被占着时返回原因
        （``updating``：项目正在更新；``occupied``：别的预约占着）。"""

        reply = await service_client("账号预约").call(
            PROJECT_RUN_RESERVE, script_request(str(self.script_info.script_id))
        )
        if reply.failed:
            # 拿不到服务（项目插件已停用或重载）：原来这里不会失败，按异常往上抛，文案是那句固定话
            raise RuntimeError(reply.message)
        if reply.skipped:
            return reply.reason or "occupied"
        self.project_lock_key = str(reply["key"])
        return None

    async def _project_context(self) -> Reply:
        """这个托管此刻的项目口径（项目服务 ``project_context``：代理、渠道、跟随来源……），每次现读配置。"""

        reply = await service_client("项目口径").call(
            PROJECT_CONTEXT,
            project_context_request(
                record_payload(str(self.script_info.script_id), self.script_config)
            ),
        )
        if reply.failed:
            raise RuntimeError(reply.message)
        return reply

    async def _release_project_path(self) -> None:
        """交回本账号的项目预约（共用库 ``release_run_reservation``）：停止时等作用域收尾、运行前设置的
        进程确认退出（运行服务 ``pretask_exit_watch``）、资源包核对做完（项目服务 ``run_release`` 等修复
        作业）再放。"""

        if self.project_lock_key is None:
            return
        key, self.project_lock_key = self.project_lock_key, None
        repair, self._repair_op = self._repair_op, None
        await release_run_reservation(
            service_client("交回预约"),
            key,
            scope=current_process_scope.get(),
            account=self._account,
            repair=repair,
            on_repair_event=self._on_service_event,
        )

    def _ensure_display_hold(self) -> None:
        """没有生效中的「保持显示开启」请求就再请求一份（每次尝试开头都调）。"""

        hold = self._display_hold
        if hold is None or not hold.active:
            self._display_hold = power.hold_display(
                f"MFW 运行（{self.cur_user_item.name}）"
            )

    async def _ensure_workstation_unlocked(self) -> None:
        """要用交互式桌面的控制器，锁屏时直接判失败，不进会重启游戏的重试。"""

        reason = await workstation_lock_reason(
            self.run_plan.controllerType if self.run_plan is not None else None,
            _desktop_process_target().get(),
        )
        if reason is not None:
            raise MaaFWWorkstationLocked(WORKSTATION_LOCKED_MESSAGE)

    def _release_display_hold(self) -> None:
        hold, self._display_hold = self._display_hold, None
        if hold is not None:
            hold.release()

    # ------------------------------------------------------------------
    # 信号、历史、记账
    # ------------------------------------------------------------------

    async def _finish_on_signal(self, attempt: int, result: MaaFWRunResult) -> None:
        """worker 回报了信号节点命中：记账、写日志状态（正式通知由管理器收尾时发）。"""

        facts = signal_attempt_facts(
            result, self.run_plan, user_messages=SIGNAL_USER_MESSAGES
        )
        signal = facts.signal
        self.run_signal = signal
        point(self, "special", state="matched", key=signal)
        message = facts.message
        logger.warning(
            f"MaaFW 账号 {self.cur_user_item.name} 命中项目信号 {signal}"
            f"（节点 {facts.node}）：{message}"
        )
        self._record_attempt(
            attempt,
            facts.labels,
            message,
            screenshots=facts.report_shots,
            category=CATEGORY_SIGNAL,
            signal=signal,
        )
        if self.cur_user_log is not None:
            await self.cur_user_log.set_state(status=message)

        project_name = ""
        if self.interface_model is not None:
            with suppress(Exception):
                project_name = await asyncio.to_thread(
                    interface_display_name, self.project_path, self.interface_model
                )
        plan = self.run_plan
        if not project_name and plan is not None:
            project_name = str(plan.projectLabel or plan.projectName or "")
        self.signal_tracker.record(
            signal,
            resource_name=facts.resource_name,
            resource_label=facts.resource_label,
            project_name=project_name,
            node=facts.node,
            user_id=str(self.cur_user_uid),
            user_name=self.cur_user_item.name,
            screenshot=facts.evidence_path,
        )

    def _is_maintenance_skip(self) -> bool:
        return self.maintenance_skipped or self.run_signal == SERVER_MAINTENANCE

    def _signal_user_message(self) -> str | None:
        """本账号因信号没跑成时给人看的原因；没有信号返回 None。"""

        if self.maintenance_skipped:
            return MAINTENANCE_SKIP_MESSAGE
        if self.run_signal is not None:
            return SIGNAL_USER_MESSAGES.get(self.run_signal, self.run_signal)
        return None

    async def _start_attempt_log(self) -> None:
        """重试前另起一份日志记录，每次尝试单独成段。"""

        start_time = datetime.now().astimezone()
        self.cur_user_log_started_at = start_time
        self.cur_user_log = LogRecord()
        self.cur_user_log = await self.cur_user_item.add_log_record(
            start_time, self.cur_user_log
        )

    def _run_record_statistic(self, key: int) -> HistorySnapshotStatistic | None:
        value = self._ledger.history_value(key)
        return HistorySnapshotStatistic(value=value) if value is not None else None

    async def _freeze_notification_images(self) -> None:
        """归档会移动临时文件，通知及跨账号报告先持有自己的图片内容。"""
        self._notification_screenshots = []
        for label, path in self._ledger.failure_screenshots(
            run_complete=self.run_complete
        )[-NOTIFY_SCREENSHOT_LIMIT:]:
            try:
                self._notification_screenshots.append(
                    (label, await asyncio.to_thread(path.read_bytes))
                )
            except OSError as error:
                self._append_log(f"冻结通知截图失败：{error}", warning=True)
        await self.signal_tracker.freeze_user_screenshots(str(self.cur_user_uid))

    async def _save_user_history(self) -> None:
        try:
            await self._save_user_history_records()
        finally:
            # 历史存完（或这一轮不存）：收尾里压住的账号日志接着写
            self._release_held_publishes()

    def _release_held_publishes(self) -> None:
        held, self._held_publishes = self._held_publishes, None
        for publish in held or ():
            self.callbacks.submit(publish())

    async def _save_user_history_records(self) -> None:
        if self._history_saved:
            return
        self._history_saved = True
        # 预检未派发的用户只保留当前报告；实际执行后遇到维护仍归档 skipped。
        if self.check_result != "Pass":
            return
        try:
            await self._freeze_notification_images()
        except Exception as error:
            self._append_log(f"冻结通知截图失败：{error}", warning=True)
            logger.opt(exception=True).warning(f"冻结通知截图失败：{error}")
        # 用户停止 / 崩溃时最后一次尝试没走到 _record_attempt：按停止或宿主异常收尾
        self._ledger.finish_open(stopped=self.stopped_manually)
        records = []
        items = list(self.cur_user_item.iter_history_logs())
        for timestamp, log_item in items:
            status = self._ledger.history_status(
                id(log_item),
                stopped=self.stopped_manually,
                maintenance=self._is_maintenance_skip(),
            )
            record = HistoryWrite(
                time=timestamp.astimezone(UTC4),
                username=self.cur_user_item.name,
                script_name=self.script_info.name,
                status=status,
                message=log_item.status,
                logs="".join(log_item.content),
            )
            run_record = self._ledger.call(
                lambda: self._run_record_statistic(id(log_item))
            )
            if run_record is not None:
                record.data[RUN_RECORD_HISTORY_KEY] = run_record
            # 附件是运行服务结果里的字典（路径是字符串）
            record.attachments.extend(
                HistoryAttachmentInput(
                    path=Path(str(item["path"])),
                    kind=item["kind"],
                    role=item["role"],
                    display_name=item["display_name"],
                )
                for item in self._ledger.attachments(id(log_item))
            )
            records.append(record)
        if records:
            await self.history.save_user_history(
                records,
                script=self.script_info,
                user=self.cur_user_item,
                log_warning=partial(self._append_log, warning=True),
            )

    def _record_attempt(
        self,
        attempt: int,
        completed_labels: list[str],
        failure: str | None,
        *,
        screenshots: list[tuple[str, Path]] | None = None,
        category: str | None = None,
        signal: str | None = None,
        user_message: str | None = None,
        failed_task: str | None = None,
    ) -> None:
        """记下本次尝试的结果（``attempt_ledger.record``），键是这次尝试的日志记录。

        这次尝试的日志还没有开始时刻时不记 history 状态。
        """

        key = id(self.cur_user_log)
        self._ledger.record(
            key,
            status_key=key if self.cur_user_log_started_at is not None else None,
            attempt=attempt,
            title=self._attempt_title,
            completed=completed_labels,
            failure=failure,
            maintenance=self.run_signal == SERVER_MAINTENANCE,
            screenshots=screenshots,
            category=category,
            signal=signal,
            user_message=user_message,
            failed_task=failed_task,
        )

    # ------------------------------------------------------------------
    # 给管理器读的报告（与通用流程同名）
    # ------------------------------------------------------------------

    def run_incident_notice(self) -> str:
        """本次运行里被原地打转检测或单任务时限停下的任务。"""

        return self._ledger.incident_notice(
            run_complete=self.run_complete,
            signal_message=self._signal_user_message(),
            check_passed=self.check_result == "Pass",
        )

    def report_screenshots(self) -> list[tuple[str, bytes]]:
        """给托管级「代理结果」报告用的失败截图，标签带上账号名称以区分多账号。"""

        return [
            (f"{self.cur_user_item.name} · {label}", path)
            for label, path in self._notification_screenshots
        ]

    def _report_result(self, *, short: bool = False) -> str:
        return self._ledger.report_result(
            short=short,
            run_complete=self.run_complete,
            missing_task_names=self.missing_task_names,
            signal_message=self._signal_user_message(),
            check_passed=self.check_result == "Pass",
            check_status=(
                self.cur_user_log.status
                if self.cur_user_log is not None
                else self.check_result
            ),
            crash_message=getattr(self, "_crash_message", None),
        )

    def proxy_report_line(self) -> str | None:
        """托管级「代理结果」里本账号的那一行；运行前检查没过、也没记日志的返回 None。"""

        if self.check_result != "Pass" and self.cur_user_log is None:
            return None
        return self._report_result(short=True)

    def missing_task_notice(self) -> str:
        """队列里 interface 已没有的任务：进用户与脚本两份报告。"""

        if not self.missing_task_names:
            return ""
        return f"{MISSING_TASK_NOTICE_PREFIX}: " + "、".join(self.missing_task_names)

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------

    async def _push_user_statistics(self) -> None:
        """按通知设置推送账号级统计信息（放在状态落定之后：结果行要读最终的 run_complete）。"""

        try:
            statistics = {}
            statistics["user_info"] = self.cur_user_item.name
            statistics["start_time"] = (
                self.user_start_time.strftime("%Y-%m-%d %H:%M:%S")
                if self.user_start_time is not None
                else ""
            )
            statistics["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            statistics["task_details"] = self._ledger.task_details(
                run_complete=self.run_complete,
                signal_message=self._signal_user_message(),
                check_passed=self.check_result == "Pass",
                missing=self.missing_task_notice(),
            )
            images = await prepare_screenshot_images(
                self._notification_screenshots,
                image_id_prefix="maafw",
            )
            statistics["screenshots"] = screenshot_entries(images)
            statistics["user_result"] = self._report_result()
            mark = statistics_mark(
                run_complete=self.run_complete,
                has_missing=bool(self.missing_task_names),
                maintenance=self._is_maintenance_skip(),
            )
            await Notify.push_statistics_report(
                payload=_format_maafw().build_statistics_payload(
                    title=statistics_title(
                        datetime.now(), mark, self.cur_user_item.name
                    ),
                    message=statistics,
                    images=[image for _, image in images],
                ),
                user_config=self.cur_user_config,
                task_info=self.task_info,
            )
        except Exception as exc:
            logger.opt(exception=True).warning(f"推送 MaaFW 统计信息时出现异常: {exc}")
            with suppress(Exception):
                await Publisher.send(
                    id=self.task_info.task_id,
                    type=protocol.TASK_NOTICE,
                    data=WSTaskNoticeData(
                        level="error",
                        message=f"推送 MaaFW 统计信息时出现异常: {exc}",
                    ),
                )

    async def _send_success_notify(self) -> None:
        try:
            title, message, ticker = success_notice_text(
                self.cur_user_item.name, self.missing_task_names
            )
            await Notify.send_notice(
                LocalNotice(
                    title=title,
                    message=message,
                    ticker=ticker,
                    duration_seconds=3,
                )
            )
        except Exception as exc:
            logger.warning(f"MaaFW 插件用户通知发送失败: {exc}")

    def _append_log(
        self, message: str, *, warning: bool = False, at: float | None = None
    ) -> None:
        """记一行账号日志。``at``：服务作业发出这一行的时刻（事件的 ``at``），时间戳按它排，轮询晚到的
        那一点不改时间戳；本地写的行不给，按此刻。"""

        # 失败类的账号日志按 WARNING 进 app.log，事后按级别筛得出来
        (logger.warning if warning else logger.info)(message)
        line = format_user_log_line(message, at=at)
        if self.cur_user_log is None:
            self._pending_user_log.append(line)
            return
        record = self.cur_user_log

        async def publish():
            await record.append_content(line)
            content = record.content
            await self.script_info.set_log("".join(content))

        if self._held_publishes is not None:
            self._held_publishes.append(publish)
            return
        self.callbacks.submit(publish())


@dataclass(frozen=True)
class _PlanHead:
    """建计划前半段交给后半段的东西（同一个 ``check`` 里，只在 ``_load_run_state`` 内部传）。"""

    interface_model: MaaFWInterface
    task_snapshot: dict[str, Any]
    controller_name: str | None
    resource_name: str | None
    script_hotkeys: Any
    inspection: MaaFWSnapshotInspection
    expansion: MaaFWSnapshotExpansion
    state: MSSArrangeState


__all__ = ["MSSAccountRun"]
