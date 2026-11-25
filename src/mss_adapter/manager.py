#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#   SPDX-License-Identifier: AGPL-3.0-or-later

"""MSS 托管的脚本级流程（管理器）。

写法照通用 MFW 的管理器（``MaaFW/embedded_manager``）逐段对应：运行前检查（打开项目、拿 X 锁、筛账号、
运行池自检）→ 运行前更新与运行环境确认 → 逐个账号建 ``account_run.MSSAccountRun`` 直接调 ``main_task`` →
收尾（保存账号配置并解锁、收尾项目、信号通知、代理结果）。差别只在：只认确切类型 ``MSSConfig``；项目会话
的请求经 MSS 自己的属主入口 ``read_managed`` 取记录组装（属主名 ``mss_adapter``）；遥测的上报名是 ``MSS``。

X 锁照旧直接 ``try_lock_x()``，不走 ``lock_script_config``；拿到锁、确认有可运行账号后照通用 MFW 调 L2 挂的
``after_config_lock``（选定游戏会话、复查预下载），账号循环里再按账号选定、跑完交回账号覆盖的目标。
打开项目、运行前更新与运行环境确认、收尾项目、队列预下载的发现都由 MFW 项目插件的服务做（``open_*``、
``update_*``、``confirm_*``、``finish_*``、``discover_*``），运行池路由与自检由 agent 环境服务做：这里只取配置、
组装只含 JSON 值的请求、调服务、把它的事件转成日志与进度点。归档、遥测、通知正文取专项共用库；读写配置一律经
``config_view``。账号流程只在账号开跑前延迟导入。不导入别的插件包（服务经 ``services.client`` 现取）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, cast

from auto_mas_core.host.config import config_manager
from auto_mas_core.host.core import Notify, Publisher
from app.core.notify.lifecycle import NotificationTaskBase
from auto_mas_core.host.core_notify_task_content import (
    NOTIFY_SCREENSHOT_LIMIT,
    prepare_screenshot_images,
    screenshot_entries,
)
from auto_mas_core.host.core_ws import protocol
from auto_mas_core.host.models_emulator import DeviceBase, DeviceProvider
from auto_mas_core.host.models_notification_report import TaskResultData, TaskResultReport
from auto_mas_core.host.models_schema import WSTaskNoticeData
from auto_mas_core.host.models_task import ScriptItem, UserItem
from maafw_adapter_kit.backup_archive import (
    archive_mas_runtime_backup,
    archive_native_backup,
)
from maafw_native.architecture import (
    describe_unsupported_architecture,
)
from maafw_adapter_kit.project_texts import (
    BENIGN_SKIPS,
    SKIP_OCCUPIED,
    describe_open_failure,
)
from maafw_adapter_kit.proxy_quota import proxy_today
from maafw_adapter_kit.run_messages import update_log_lines
from maafw_adapter_kit.run_notices import (
    proxy_result_body,
    signal_summary_title,
    user_results_text,
)
from maafw_adapter_kit.owner_entry import record_payload, write_or_raise
from maafw_adapter_kit.services import (
    AGENT_ENV_RUNTIME_CHECK,
    AGENT_ENV_RUNTIME_ROUTE,
    PROJECT_CONFIRM,
    PROJECT_CONTEXT,
    PROJECT_FINISH,
    PROJECT_UPDATE_DISCOVER,
    PROJECT_UPDATE_UPDATE,
    Operation,
    ProjectOpener,
    ProjectOpenResult,
    Reply,
    confirm_request,
    event_dispatcher,
    finish_request,
    open_project,
    open_request,
    project_context_request,
    project_job_request,
    runtime_check_request,
    update_request,
)
from maafw_interface.runner.models import SERVER_MAINTENANCE
from maafw_adapter_kit.signal_notice import (
    MaaFWSignalTracker,
    push_signal_notices,
)
from maafw_adapter_kit.telemetry_project import record_project_run
from mss_adapter import config_view
from mss_adapter.owner import OWNER
from mss_adapter.services import PLUGIN_ID
from mss_adapter.services import client as service_client
from auto_mas_core.adapters.script import point
from auto_mas_core.host.utils import TASK_MODE_ZH, get_logger

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，运行期不导入 maa
    from mss_adapter.account_run import MSSAccountRun

# 与通用 MFW 的管理器同名：app.log 的检索习惯不变
logger = get_logger("MFW 内置运行")

# 遥测里的专项上报名（telemetry_project.record_project_run）
_TELEMETRY_SCRIPT_TYPE = "MSS"

#: 项目自动更新的时机（项目服务 ``project_context`` 回的 ``auto_update_mode``）。
AutoUpdateMode = Literal["Off", "BeforeRun", "AfterRun"]


def _current_game_session() -> Any:
    """宿主 L2 的游戏会话（``current_game_session``，经 SDK 出口）：用到时现取。"""

    from auto_mas_core.host.task_game import current_game_session

    return current_game_session


class _ManagedRecordMissing(RuntimeError):
    """MSS 属主读不到这个托管（多半是运行中被删了）。"""


def _managed_record(script_id: str) -> dict[str, Any]:
    """经 MSS 的属主读这个托管的记录（共用库 ``record_payload`` 的字典形，配置原值；项目服务各操作的
    ``record``）。读不到时抛 :class:`_ManagedRecordMissing`。"""

    found = OWNER.read({"script_id": script_id})
    record = found.get("record")
    if record is None:
        raise _ManagedRecordMissing(
            f"MSS 属主读不到托管 {script_id}：{found.get('status')}"
        )
    return record


class MSSManager(NotificationTaskBase):
    """MSS 托管的脚本级管理器：装配脚本配置、账号配置和模拟器实例，逐个账号交给
    ``MSSAccountRun``。"""

    def __init__(
        self,
        script_info: ScriptItem,
        *,
        device_provider: DeviceProvider | None = None,
        host_locked_config: Any = None,
    ):
        super().__init__()

        # 插件路径（``expander.MSSExpander``）：宿主 L2 已经拿着这个托管的 X 锁（不可重入），
        # ``_check`` 就不再自己 ``try_lock_x()``，也不往 ``config_locks`` 里放票；锁由 L2 释放。
        self._host_locked_config = host_locked_config
        if script_info.task_info is None:
            raise RuntimeError("ScriptItem 未绑定到 TaskItem")

        self.task_info = script_info.task_info
        self.script_info = script_info
        self.check_result: str = "-"
        self.begin_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 托管配置与账号配置只当句柄，读写经 config_view
        self.script_config: Any = None
        self.user_config: Any = None
        self.runnable_user_uids: list[uuid.UUID] = []
        self.emulator_manager: DeviceBase | None = None
        self._device_provider = device_provider
        # check() 获取 X 锁，运行及用户收尾直接修改原配置，最后释放。
        self._script_locked = False
        # 当前正在跑的那一位用户的账号任务；每个账号各建一个。
        self.inner_task: "MSSAccountRun | None" = None
        self._inner_finalized = True
        self._report_finalized = False
        # 各账号跑完攒下的失败截图（带账号名称的标签, 内容），最后随「代理结果」发出。
        self._failure_screenshots: list[tuple[str, bytes]] = []
        # 各账号队列里的失效任务，随「代理结果」发出
        self._missing_task_notices: list[str] = []
        self._missing_task_user_ids: set[str] = set()
        # 各账号被原地打转检测 / 单任务时限停下过的任务（只在该账号最后没成功时列）
        self._run_incident_notices: list[str] = []
        # 各账号在「代理结果」里的那一行（账号 id → 结果）
        self._user_report_lines: dict[str, str] = {}
        # 本轮项目信号的账，各账号共用一份
        self.signal_tracker = MaaFWSignalTracker()
        # 项目更新的日志行（已带时间戳）；运行前更新的会并入第一位用户的日志。
        self.project_update_logs: list[str] = []
        self._auto_update_mode: AutoUpdateMode = "Off"
        # 项目服务（打开 / 更新 / 环境确认 / 收尾 / 预下载的发现）：过程输出经 _on_project_event 回来。
        # 收尾期间用户点了停止（_on_finalize_cancel_requested）：在途的更新 / 环境确认经 _stop 停下，
        # 之后才开始的带 stop_requested 开始（原来项目会话的 stop()）。
        self._project_stop_requested = False
        self._project_ops: set[Operation] = set()
        # 打开项目成功时的租约（``view_path`` / ``interface_path`` / ``version``），运行前自检用。
        self._project_lease: Mapping[str, Any] | None = None
        # 只有 main_task 正常跑完全部用户才置位；取消/崩溃路径不跑运行后更新。
        self._users_completed = False
        # 运行前检查时视图标记显示还欠一次运行环境确认：main_task 据此在账号任务之前确认。
        self._env_confirm_needed = False
        # 本轮开始时刻：收尾时只清在它之前轮转出来的原生日志备份。
        self._round_started_at = time.time()
        self._view_maintained = False
        # 收尾组同步切了版本、还欠一次运行环境确认：放到代理结果推送之后再做。
        self._post_run_env_confirm = False

    # ------------------------------------------------------------------
    # 运行前检查
    # ------------------------------------------------------------------

    async def check(self) -> str:
        """校验运行的前置条件，返回 ``"Pass"`` 或用户可读的原因（没通过也记一行后端日志）。"""

        # 预取会提前构造管理器；报告与本轮日志清理从真正开始运行前检查时计时。
        self.begin_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._round_started_at = time.time()
        result = await self._check()
        if result != "Pass":
            logger.info(
                f"MFW 内置运行前检查未通过（{self.script_info.name}）：{result}"
            )
        return result

    async def _check(self) -> str:
        if self.execution_mode != "AutoProxy":
            return "MFW 内置运行当前仅支持自动代理模式"

        try:
            script_uid = uuid.UUID(self.script_info.script_id)
        except (ValueError, AttributeError, TypeError):
            return "MFW 托管 ID 无效，请刷新后重试"

        try:
            script_config = config_view.lookup_script(script_uid)
        except (KeyError, ValueError):
            return "MFW 托管配置不存在，请刷新后重试"

        # 按确切类型认（不靠继承）：这个管理器只跑 MSS 托管。
        if not config_view.is_mss_script(script_config):
            return "托管配置类型错误，不是 MFW 托管类型"
        self.script_config = script_config

        # 只支持 x64：不是就在导入、更新、准备运行环境之前说一次，本轮整个脚本不跑。
        unsupported = await asyncio.to_thread(describe_unsupported_architecture)
        if unsupported:
            return unsupported

        script_id = str(self.script_info.script_id)

        async def prepare_before_lock(
            opener: ProjectOpener, opened: ProjectOpenResult
        ) -> str | None:
            if opened.record_update is not None:
                # 视图已变，取消时也先把导入报告写完，再交回项目预约。写回经本插件自己的属主。
                # 插件路径上 L2 已持托管 X 锁（票在本上下文里）：带 holds_lock 直接写节点，
                # 宿主 update_script 对锁着的托管一律拒绝。
                await write_or_raise(
                    OWNER,
                    script_id,
                    opened.record_update,
                    holds_lock=self._host_locked_config is not None,
                )
            if opener.stopping:
                return None
            self._env_confirm_needed = opened.env_confirm_needed
            if opened.kind != "opened":
                return opened.reason
            self._project_lease = opened.lease
            # 新账号的默认队列也会在线程里读取视图，继续持预约直到取到 X。
            await config_view.fill_default_snapshots(script_id, script_config)
            return None

        async def lock_before_release() -> str | None:
            # X 票留在管理器任务上下文；取得后才释放项目预约，两者连续交接。
            # 照旧直接 try_lock_x()，不走 lock_script_config。
            # 宿主已持锁（插件路径）时跳过取锁，只记「已锁」。
            if self._host_locked_config is None:
                ticket = await script_config.try_lock_x()
                if ticket is None:
                    return SKIP_OCCUPIED
                self.config_locks[config_view.script_uid(script_config)] = ticket
            self._script_locked = True
            return None

        # 打开项目（项目服务 open_*，经共用库 open_project）：导入期间持有项目预约，拿不到就整脚本良性
        # 跳过。用户停止时等这一整段做完（有限宽限）再交回预约。请求的记录经 MSS 属主现读（读不到同原来
        # 打开项目时出错，说 describe_open_failure 那句）。
        try:
            record = _managed_record(script_id)
        except Exception as exc:  # noqa: BLE001 - 同原来打开项目里的出错
            return describe_open_failure(script_id, exc)
        problem = await open_project(
            service_client("打开项目"),
            open_request(
                script_id=script_id,
                owner=PLUGIN_ID,
                record=record,
                script_name=str(self.script_info.name or ""),
                round_started_at=self._round_started_at,
            ),
            inside=prepare_before_lock,
            after=lock_before_release,
            on_event=self._on_project_event,
        )
        if problem is not None:
            return problem
        assert self._project_lease is not None
        project_root = Path(str(self._project_lease["view_path"]))
        user_config = config_view.user_collection(script_config)
        self.user_config = user_config

        # 只跑「已启用且剩余天数未耗尽」的用户；单独运行指定了用户时只保留该账号。
        self.runnable_user_uids = [
            uid
            for uid, cfg in config_view.user_items(user_config)
            if config_view.runnable(cfg) and self.task_info.is_target_user(str(uid))
        ]
        if not self.runnable_user_uids:
            return "MFW 没有可运行的账号，请在账号管理页添加并启用至少一个账号"

        # 与旧专项 lock_script_config 同一约定：拿到托管锁后由 L2 选定游戏会话（游戏 /
        # 模拟器）、复查预下载；不调的话整轮读不到绑定的游戏与模拟器。放在项目预约交回
        # 之后（选定可能要等串行游戏），也放在确认有可运行账号之后（没有账号就不先占
        # 游戏、不排队），失败时还锁、照实说原因。
        callback = getattr(self, "after_config_lock", None)
        if callback is not None:
            try:
                await callback(script_config)
            except BaseException as exc:
                from auto_mas_core.host.utils_common_fluxdown_cli import FluxdownCancelUnconfirmed

                ticket = self.config_locks.pop(
                    config_view.script_uid(script_config), None
                )
                self._script_locked = False
                if ticket is not None:
                    await script_config.unlock(ticket)
                if not isinstance(exc, Exception) or isinstance(
                    exc, FluxdownCancelUnconfirmed
                ):
                    raise
                logger.opt(exception=True).warning(
                    f"MFW 取得托管锁后准备运行失败（{script_id}）：{exc}"
                )
                return str(exc) or type(exc).__name__

        self.emulator_manager = await self._resolve_emulator_manager(script_config)

        # 自检看的是有效根：内嵌脚本的运行池版本钉在副本的投影标记上，不在来源目录。
        # agent 环境服务在工作线程里查（同原来 asyncio.to_thread）。
        reply = await service_client("运行前检查").call(
            AGENT_ENV_RUNTIME_CHECK, runtime_check_request(project_root)
        )
        if reply.failed:
            # 拿不到服务（agent 环境插件已停用或重载）：原来这里的异常往上抛，文案换成那句固定话
            raise RuntimeError(reply.message)
        environment_problem = reply.get("problem")
        if environment_problem:
            return environment_problem

        return "Pass"

    async def _resolve_emulator_manager(self, script_config: Any) -> DeviceBase | None:
        """按托管级模拟器配置取实例；未配置时返回 None（ADB 缺模拟器由账号流程报）。"""

        emulator_id = str(config_view.emulator_target(script_config) or "").strip()
        if not emulator_id or emulator_id == "-":
            return None

        from auto_mas_core.host.core import GameManager

        device_provider = self._device_provider or GameManager.get_emulator_instance
        try:
            return await device_provider(emulator_id)
        except Exception as exc:  # noqa: BLE001 - 缺模拟器不该拦住 Win32 项目
            logger.warning(f"MFW 内置运行取模拟器实例失败，将按无模拟器继续：{exc}")
            return None

    @staticmethod
    async def _runtime_pool_route() -> SimpleNamespace:
        """经 agent 环境服务解析 Runtime Pool 路由，交回 ``root``（``Path``）与 ``pool_id``。

        服务在工作线程里取（可能等运行池的进程锁）；路由失败时原来往上抛的异常换成同一句话的
        ``RuntimeError``（实施方案 §2.2）。
        """

        reply = await service_client("运行池路由").call(AGENT_ENV_RUNTIME_ROUTE)
        if reply.failed:
            raise RuntimeError(reply.message)
        return SimpleNamespace(root=Path(reply["root"]), pool_id=reply["pool_id"])

    async def _resolve_auto_update_mode(self) -> AutoUpdateMode:
        """这个托管的项目自动更新时机（项目服务 ``project_context``，与项目更新自己同一口径）。"""

        assert self.script_config is not None
        reply = await service_client("项目口径").call(
            PROJECT_CONTEXT,
            project_context_request(
                record_payload(str(self.script_info.script_id), self.script_config)
            ),
        )
        if reply.failed:
            raise RuntimeError(reply.message)
        return cast(AutoUpdateMode, reply["auto_update_mode"])

    def _build_inner_task(self, route: Any) -> "MSSAccountRun":
        """``route`` 是 :meth:`_runtime_pool_route` 的结果（``root``、``pool_id``）。"""

        # 延迟导入：账号流程经 runner 包 import maa，导入即打开 DLL。
        from mss_adapter.account_run import MSSAccountRun

        assert self.script_config is not None
        assert self.user_config is not None
        task = MSSAccountRun(
            self.script_info,
            self.script_config,
            config_view.user_entries(self.user_config),
            self.emulator_manager,
            # 运行前更新的日志只并入第一位用户；取走后列表清空，后续账号不重复。
            project_update_logs=self._take_project_update_logs(),
            signal_tracker=self.signal_tracker,
        )
        task.history = self.history
        task.callbacks = self.callbacks
        task.maafw_runtime_pool_root = route.root
        task.maafw_runtime_pool_id = route.pool_id
        return task

    # ------------------------------------------------------------------
    # 项目服务
    # ------------------------------------------------------------------

    @staticmethod
    async def check_prefetch(config) -> list:
        """按项目更新模式和凭据发现可用更新，跳过跟随源码的项目并返回发行文件下载需求。

        项目更新服务的 ``discover_*`` 做（会联网），请求的记录经 MSS 属主现读；它回的下载需求字典还原成
        宿主的 ``DownloadRequest``。服务回失败时按 §2.2 抛同一句话的 ``RuntimeError``。
        """

        from auto_mas_core.host.models_schema import DownloadRequest

        script_id = str(config_view.script_uid(config))
        reply = await service_client("队列预下载").run(
            PROJECT_UPDATE_DISCOVER,
            project_job_request(
                script_id=script_id,
                owner=PLUGIN_ID,
                record=_managed_record(script_id),
            ),
        )
        if reply.failed:
            raise RuntimeError(reply.message)
        return [
            DownloadRequest.model_validate(item)
            for item in reply.get("downloads") or []
        ]

    def _take_project_update_logs(self) -> list[str]:
        logs, self.project_update_logs = self.project_update_logs, []
        return logs

    def _append_update_log(self, message: str, *, at: float | None = None) -> None:
        """记一行更新日志：后端日志 + 脚本实时日志 + 待并入账号日志的缓冲。

        ``at`` 是项目服务发出这一行的时刻（事件的 ``at``），时间戳按它排，轮询晚到的那一点不改时间戳。
        """

        logger.info(f"MFW 项目更新：{message}")
        when = (
            datetime.now().astimezone()
            if at is None
            else datetime.fromtimestamp(at).astimezone()
        )
        lines = update_log_lines(message, when)
        if lines is None:
            # 详情行只进后端日志，不上界面与账号日志。
            return
        self.project_update_logs.extend(lines)
        self.callbacks.submit(
            self.script_info.set_log("".join(self.project_update_logs))
        )

    def _on_project_event(self, event: Mapping[str, Any]) -> None:
        """项目服务作业的事件出口：日志行进项目更新日志，进度点以管理器为 owner 上报，预下载的
        字节进度转给下载观察者。在事件循环线程里同步调用。"""

        def log(
            message: str, *, warning: bool = False, at: float | None = None
        ) -> None:
            self._append_update_log(message, at=at)

        def progress_point(stage: Any, *, state: str, key: str) -> None:
            point(self, stage, state=state, key=key)

        def download(received: Any, total: Any) -> None:
            from auto_mas_core.host.core_downloads import download_observer

            observer = download_observer.get()
            if observer is not None:
                observer(Path("项目更新包"), received, total, 0.0)

        event_dispatcher(log=log, point=progress_point, download=download)(event)

    def _record(self) -> dict[str, Any]:
        """本托管此刻的托管记录（经 MSS 属主现读；读不到抛 :class:`_ManagedRecordMissing`）。"""

        return _managed_record(str(self.script_info.script_id))

    async def _notify_update(
        self, level: Literal["info", "warning", "error"], message: str
    ) -> None:
        await Publisher.send(
            id=self.task_info.task_id,
            type=protocol.TASK_NOTICE,
            data=WSTaskNoticeData(level=level, message=message),
        )

    def _on_finalize_cancel_requested(self) -> None:
        """收尾期间用户点了停止：让运行后更新 / 环境确认按停止处理（原来项目会话的 stop()）。

        同步执行、不等：在途的更新 / 环境确认经共用库 ``Operation.stop()`` 发 ``_stop``；之后才开始
        的带 ``stop_requested`` 开始（:meth:`_project_operation`）。
        """

        self._project_stop_requested = True
        for operation in list(self._project_ops):
            operation.stop()
        if self.inner_task is not None and not self._inner_finalized:
            self.inner_task.stopped_manually = True
            self.inner_task._on_finalize_cancel_requested()

    def _stop_seen(self) -> bool:
        """用户已经要求停止（停在主任务里，或收尾进行中又点了停止）。"""

        return self.stopped_manually or self._project_stop_requested

    async def _project_operation(
        self, family: str, request: Mapping[str, Any]
    ) -> Reply:
        """跑一个能被「收尾期间停止」停下的项目长操作（运行前 / 后更新、环境确认）。"""

        operation = service_client("项目更新").operation(
            family, on_event=self._on_project_event
        )
        if self._project_stop_requested:
            operation.stop()
        self._project_ops.add(operation)
        try:
            return await operation.run(request)
        finally:
            self._project_ops.discard(operation)

    async def _post_run_view_maintenance(self) -> None:
        """收尾项目：组同步（只在账号全部正常跑完时做）、写穿巡检、原生日志备份清理。"""

        assert self.script_config is not None
        sync_group = self._users_completed and not self._stop_seen()
        # 收尾项目（项目服务 finish_*）：组同步、写穿巡检、清本轮开始前轮转出来的原生日志备份
        reply = await service_client("收尾项目").run(
            PROJECT_FINISH,
            finish_request(
                script_id=str(self.script_info.script_id),
                owner=PLUGIN_ID,
                script_name=str(self.script_info.name or ""),
                record=self._record(),
                sync_group=sync_group,
                round_started_at=self._round_started_at,
            ),
        )
        if reply.failed:
            # 原来收尾出错时异常往上抛，final_task 记「MFW 收尾维护失败」
            raise RuntimeError(reply.message)
        if sync_group:
            # 切了版本要确认一次运行环境，放到代理结果推送之后由 final_task 做。
            self._post_run_env_confirm = bool(reply.get("env_confirm_needed"))

    async def _run_project_update(self, phase: AutoUpdateMode) -> None:
        """按时机更新项目目录（项目更新服务 ``update_*``）。任何失败都只记日志 + 通知，不抛出。"""

        assert self.script_config is not None
        params = self.script_info.info.params or {}
        reply = await self._project_operation(
            PROJECT_UPDATE_UPDATE,
            update_request(
                script_id=str(self.script_info.script_id),
                owner=PLUGIN_ID,
                script_name=str(self.script_info.name or ""),
                record=self._record(),
                phase=phase,
                prefetch_paths=params.get("prefetch_paths", {}),
                round_started_at=self._round_started_at,
            ),
        )
        await self._send_project_notice(reply)

    async def _ensure_project_environment(self, phase: AutoUpdateMode) -> None:
        """更新之后确认一次运行环境（项目服务 ``confirm_*``）。任何失败都只记日志 + 通知，不抛出。"""

        assert self.script_config is not None
        reply = await self._project_operation(
            PROJECT_CONFIRM,
            confirm_request(
                script_id=str(self.script_info.script_id),
                owner=PLUGIN_ID,
                record=self._record(),
                phase=phase,
            ),
        )
        await self._send_project_notice(reply)

    async def _send_project_notice(self, reply: Reply) -> None:
        """更新 / 环境确认的终态里带的通知（``notice``）照原样发；拿不到服务时只记后端日志。"""

        if reply.failed:
            logger.warning(f"MFW 项目更新服务没有完成：{reply.message}")
            return
        notice = reply.get("notice")
        if isinstance(notice, Mapping):
            await self._notify_update(notice["level"], str(notice["message"]))

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    async def main_task(self) -> None:
        self.check_result = await self.check()
        if self.check_result != "Pass":
            benign = self.check_result in BENIGN_SKIPS
            await self.script_info.set_state(status="跳过" if benign else "异常")
            await Publisher.send(
                id=self.task_info.task_id,
                type=protocol.TASK_NOTICE,
                data=WSTaskNoticeData(
                    level="warning" if benign else "error", message=self.check_result
                ),
            )
            return

        # AutoProxy 的实际账号列表在这里加载。
        assert self.user_config is not None
        assert self.script_config is not None
        await self.script_info.set_users(
            [
                UserItem(
                    user_id=str(uid),
                    name=config_view.user_name(self.user_config[uid]),
                    status="等待",
                )
                for uid in self.runnable_user_uids
            ]
        )
        await self.script_info.set_state(status="运行")
        logger.info(
            f"MFW 内置运行账号列表加载完成，已筛选账号数: "
            f"{len(self.script_info.user_list)}"
        )

        # 运行前归档项目配置（config/），归档的是有效根；失败不阻断任务。
        try:
            archive_native_backup(
                self.script_info.script_id,
                config_view.project_root(
                    str(self.script_info.script_id), self.script_config
                ),
            )
        except Exception:
            logger.opt(exception=True).warning(
                "MaaFW 运行前项目配置归档失败，已跳过（不阻断任务）"
            )

        # 运行前更新：整个脚本一次，在第一位用户的账号任务建起来之前；更新完接着确认运行环境。
        # 账号全都今日代理次数已满时这一轮谁也不会跑：两步都不做、不联网。
        self._auto_update_mode = await self._resolve_auto_update_mode()
        today = proxy_today()
        if all(
            config_view.proxy_times_used_up(
                self.user_config[uid], self.script_config, today
            )
            for uid in self.runnable_user_uids
        ):
            logger.info(
                f"MFW 本轮账号今日代理次数都已达上限（{self.script_info.name}），"
                "跳过运行前更新与运行环境确认"
            )
        elif self._auto_update_mode == "BeforeRun":
            await self._run_project_update("BeforeRun")
            await self._ensure_project_environment("BeforeRun")
        elif self._env_confirm_needed:
            # 视图挂的版本还没确认过运行环境：在账号任务之前把环境备好。
            await self._ensure_project_environment("BeforeRun")

        await self._record_project_run()

        # main_task / final_task 都是按账号的，因此每个账号各建一个。
        for index in range(len(self.runnable_user_uids)):
            await self.script_info.set_current_index(index)
            user_id = self.runnable_user_uids[index]
            # 物化前归档本账号 MAS 字段侧车（失败只记日志不阻断任务）
            try:
                archive_mas_runtime_backup(
                    self.script_info.script_id,
                    str(user_id),
                    overlay=config_view.overlay_values(self.user_config[user_id]),
                )
            except Exception:
                logger.opt(exception=True).warning(
                    "MaaFW 运行前字段侧车归档失败，已跳过（不阻断任务）"
                )
            # 按账号选定游戏会话，包住该账号的全部尝试与收尾（关游戏、关模拟器都在
            # _finalize_inner_task 里），账号另绑的目标跑完当场交回。
            try:
                try:
                    selected = await self._select_account_game(index, user_id)
                except asyncio.CancelledError:
                    self.stopped_manually = True
                    raise
                if not selected:
                    continue
                route = await self._runtime_pool_route()
                self.inner_task = self._build_inner_task(route)
                self._inner_finalized = False
                try:
                    await self.inner_task.main_task()
                except asyncio.CancelledError:
                    self.stopped_manually = True
                    raise
                # 只截 Exception：CancelledError 属 BaseException，必须继续外抛。
                except Exception as exc:  # noqa: BLE001
                    await self.inner_task.on_crash(exc)
                finally:
                    await self._finalize_inner_task()
                    self._reclaim_unused_update_logs()
            finally:
                session = _current_game_session().get()
                if session is not None:
                    await session.release_account()
        self._users_completed = True

    async def _select_account_game(self, index: int, user_id: uuid.UUID) -> bool:
        """按账号选定游戏会话；选定失败时只把这个账号记异常并说明原因，返回 False。

        与旧专项 ``TaskExecuteBase.spawn`` 同一约定：账号绑定为「-」时沿用托管级目标
        （目标没变，``select`` 直接返回）；另绑了目标就换成它的，失败交回会话、继续下一个
        账号。选定之后重取模拟器：账号任务开关模拟器、取 ADB 都要用这次选定的那台，
        留底的实例不能还是托管级或上一个账号的。
        """

        session = _current_game_session().get()
        if session is None:
            return True
        assert self.user_config is not None
        assert self.script_config is not None
        owner = self.user_config[user_id]
        if self._account_emulator_without_device(owner):
            # 另绑了模拟器却没选实例：会话只订阅游戏、没有设备，之后设备会回落到托管的，
            # 运行前检查放行、到取 ADB 才报错并把用户指向托管页，这里直接按选定失败处理。
            await session.release()
            await self._fail_account_game(
                index,
                user_id,
                "账号绑定的模拟器未选择实例，请在账号的「账号绑定」里选择设备",
            )
            return False
        try:
            await session.select(owner)
        except Exception as exc:  # noqa: BLE001 - 目标已删、设备失效、绑定被拒
            await session.release()
            await self._fail_account_game(
                index, user_id, str(exc) or type(exc).__name__
            )
            return False
        self.emulator_manager = await self._resolve_emulator_manager(self.script_config)
        return True

    @staticmethod
    def _account_emulator_without_device(owner: Any) -> bool:
        """账号另绑了模拟器目标、设备却是「-」；目标已不存在时不拦，留给 select 报原因。"""

        return config_view.account_emulator_without_device(owner)

    async def _fail_account_game(
        self, index: int, user_id: uuid.UUID, reason: str
    ) -> None:
        """账号游戏目标不可用：只把这个账号记异常、写明原因，其余账号照常运行。"""

        user_item = self.script_info.user_list[index]
        logger.warning(
            f"MFW 账号游戏目标不可用（{self.script_info.name} / {user_item.name}）："
            f"{reason}"
        )
        message = f"账号 {user_item.name} 未运行，游戏目标不可用：{reason}"
        await user_item.set_state(status="异常")
        self._user_report_lines[user_item.user_id] = f"游戏目标不可用：{reason}"
        # 选定中途失败会停在「等待」：其余账号照常运行
        if self.script_info.status == "等待":
            await self.script_info.set_state(status="运行")
        await self.script_info.set_log(message)
        await Publisher.send(
            id=self.task_info.task_id,
            type=protocol.TASK_NOTICE,
            data=WSTaskNoticeData(level="error", message=message),
        )

    def _reclaim_unused_update_logs(self) -> None:
        """这位用户没进入运行时，运行前更新的日志拿回来交给下一位用户。"""

        inner = self.inner_task
        if inner is None:
            return
        logs = list(getattr(inner, "project_update_logs", None) or [])
        user_item = getattr(inner, "cur_user_item", None)
        if not logs or user_item is None or getattr(user_item, "log_record", None):
            return
        self.project_update_logs = logs + self.project_update_logs

    async def _record_project_run(self) -> None:
        """记一次项目运行（``telemetry_project.record_project_run``）；遥测失败不影响任务。"""

        assert self.script_config is not None
        project_path: Path | None
        try:
            project_path = config_view.project_root(
                str(self.script_info.script_id), self.script_config
            ).resolve()
        except Exception:  # noqa: BLE001 - 遥测失败不影响任务，项目记 other
            project_path = None
        await record_project_run(project_path, script_type=_TELEMETRY_SCRIPT_TYPE)

    async def _finalize_inner_task(self) -> None:
        """收尾当前账号的账号任务；对同一个任务只做一次。"""

        if self.inner_task is None or self._inner_finalized:
            return
        self._inner_finalized = True
        self.inner_task.stopped_manually = self.stopped_manually
        try:
            await self.inner_task._run_final_task()
        except Exception as exc:  # noqa: BLE001
            logger.opt(exception=True).warning(f"MFW 内置运行收尾异常：{exc}")
        finally:
            # 基座会在收尾完成后重新抛出取消；已冻结的用户报告仍须归入脚本报告。
            with suppress(Exception):
                self._failure_screenshots.extend(self.inner_task.report_screenshots())
            with suppress(Exception):
                notice = self.inner_task.missing_task_notice()
                if notice:
                    user_item = self.inner_task.cur_user_item
                    self._missing_task_notices.append(f"{user_item.name}: {notice}")
                    self._missing_task_user_ids.add(user_item.user_id)
            with suppress(Exception):
                line = self.inner_task.proxy_report_line()
                if line is not None:
                    user_item = self.inner_task.cur_user_item
                    self._user_report_lines[user_item.user_id] = line
            with suppress(Exception):
                incidents = self.inner_task.run_incident_notice()
                if incidents:
                    user_item = self.inner_task.cur_user_item
                    self._run_incident_notices.extend(
                        f"{user_item.name}: {line}" for line in incidents.splitlines()
                    )

    async def _commit_user_data(self) -> None:
        """保存已提交的运行状态，并在异常或取消时也释放 X 锁。"""
        if not self._script_locked:
            return
        assert self.script_config is not None
        try:
            await config_manager.flush()
        except Exception as exc:  # noqa: BLE001
            logger.opt(exception=True).warning(f"MFW 账号配置保存失败：{exc}")
        finally:
            ticket = self.config_locks.pop(
                config_view.script_uid(self.script_config), None
            )
            if ticket is not None:
                await self.script_config.unlock(ticket)
            self._script_locked = False

    async def final_task(self) -> None:
        # 正常路径下每个账号跑完就已收尾；这里只兜取消与异常路径的最后一位用户。
        try:
            await self._finalize_inner_task()
        finally:
            # 最后一位用户收尾后保存状态；取消与崩溃路径也从这里释放锁。
            await self._commit_user_data()
        # 手动停止时还没轮到的用户保留「等待」，只有正在跑的那位按中断记「异常」。
        for user in self.script_info.user_list:
            if user.status == "运行" or (
                user.status == "等待" and not self.stopped_manually
            ):
                await user.set_state(status="异常")
        waiting_users = [
            user for user in self.script_info.user_list if user.status == "等待"
        ]

        # 脚本终态必须在这里落定。
        error_users = [
            user for user in self.script_info.user_list if user.status == "异常"
        ]
        completed_users = [
            user for user in self.script_info.user_list if user.status == "完成"
        ]
        # 因停服维护跳过的账号：不算完成，也不算异常
        maintenance_ids = self.signal_tracker.user_ids(SERVER_MAINTENANCE)
        maintenance_users = [
            user
            for user in self.script_info.user_list
            if user.status == "跳过" and user.user_id in maintenance_ids
        ]
        if self.check_result in BENIGN_SKIPS:
            # 项目被暂时占着、整脚本跳过：不是故障。
            await self.script_info.set_state(status="跳过")
        elif self.check_result == "Pass" and not error_users and not waiting_users:
            # 全员因维护跳过时不能显示成「完成」
            await self.script_info.set_state(
                status="跳过" if maintenance_users and not completed_users else "完成"
            )
        else:
            await self.script_info.set_state(status="异常")

        if self.check_result != "Pass":
            return
        # 账号状态已保存、脚本已解锁：收尾项目，只做一次（final_task 可能被取消路径再调）。
        if not self._view_maintained:
            self._view_maintained = True
            try:
                await self._post_run_view_maintenance()
            except Exception as exc:  # noqa: BLE001
                logger.opt(exception=True).warning(f"MFW 收尾维护失败：{exc}")
        if self._report_finalized:
            return
        self._report_finalized = True

        await self._push_signal_notices()

        # 全员都因停服维护跳过：信号通知已经说清楚了，不再发「代理结果」汇总。
        if maintenance_users and len(maintenance_users) == len(
            self.script_info.user_list
        ):
            logger.info(
                f"MFW 本轮账号全部因停服维护跳过（{self.script_info.name}），"
                "只发信号通知，不发代理结果汇总"
            )
        else:
            await self._push_proxy_report(
                error_users=error_users,
                completed_users=completed_users,
                maintenance_users=maintenance_users,
                waiting_users=waiting_users,
            )

        # 收尾组同步可能切到兄弟脚本已经更新的版本，报告发出后补充确认环境。
        if not self._users_completed or self._stop_seen():
            return
        if self._post_run_env_confirm:
            try:
                await self._ensure_project_environment("AfterRun")
            except _ManagedRecordMissing as exc:
                # 收尾在保存账号配置并解锁之后：托管这时被删了，记一行、跳过这一步，不把本轮
                # 结果改成异常（运行阶段读不到照旧抛）。旧路径拿手里的配置对象照常确认。
                logger.warning(f"MFW 收尾时托管已不在，跳过运行环境确认：{exc}")

    async def _push_proxy_report(
        self,
        *,
        error_users: list[UserItem],
        completed_users: list[UserItem],
        maintenance_users: list[UserItem],
        waiting_users: list[UserItem] | None = None,
    ) -> None:
        """推送本轮的「代理结果」汇总。``waiting_users``：手动停止时还没轮到的用户。"""

        # 跑完了但队列里有失效任务的账号：汇总里算未完成
        attention_users = [
            user
            for user in completed_users
            if user.user_id in self._missing_task_user_ids
        ]
        completed_users = [
            user for user in completed_users if user not in attention_users
        ]
        mode_zh = TASK_MODE_ZH[self.execution_mode]
        script_name = self.script_info.name or "空白"
        title = f"{datetime.now().strftime('%m-%d')} | {script_name}的{mode_zh}任务报告"
        end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        results_text = user_results_text(
            [
                (user.user_id, user.name, user.result)
                for user in self.script_info.user_list
            ],
            self._user_report_lines,
            self.script_info.result,
        )
        result = cast(
            TaskResultData,
            proxy_result_body(
                mode_zh=mode_zh,
                script_name=script_name,
                begin_time=self.begin_time,
                end_time=end_time,
                completed=len(completed_users),
                uncompleted=(
                    len(error_users)
                    + len(maintenance_users)
                    + len(attention_users)
                    + len(waiting_users or [])
                ),
                results_text=results_text,
                incident_lines=self._run_incident_notices,
                missing_lines=self._missing_task_notices,
                summary_title=signal_summary_title(
                    title,
                    {record.signal for record in self.signal_tracker.records()},
                    has_error=bool(error_users),
                    has_completed=bool(completed_users),
                ),
            ),
        )
        try:
            images = await prepare_screenshot_images(
                self._failure_screenshots[-NOTIFY_SCREENSHOT_LIMIT:],
                image_id_prefix="maafw",
            )
            result["screenshots"] = screenshot_entries(images)
            await Notify.push_proxy_result(
                title=title,
                report=TaskResultReport.from_message(result),
                task_info=self.task_info,
                images=[image for _, image in images],
                include_system=not self.signal_tracker.records(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.opt(exception=True).warning(f"推送 MFW 代理结果时出现异常: {exc}")
            await Publisher.send(
                id=self.task_info.task_id,
                type=protocol.TASK_NOTICE,
                data=WSTaskNoticeData(
                    level="error", message=f"推送 MFW 代理结果时出现异常: {exc}"
                ),
            )

    async def _push_signal_notices(self) -> None:
        """本轮撞上的停服维护 / 需要更新客户端，每个「信号 + 资源」发一条通知。"""

        if not self.signal_tracker.records():
            return
        user_config = self.user_config

        def user_config_for(user_id: str) -> Any | None:
            if user_config is None:
                return None
            try:
                return user_config[uuid.UUID(user_id)]
            except (KeyError, ValueError):
                return None

        try:
            await push_signal_notices(
                self.signal_tracker,
                script_name=self.script_info.name or "空白",
                user_config_for=user_config_for,
            )
        except Exception as exc:  # noqa: BLE001
            logger.opt(exception=True).warning(f"推送 MFW 信号通知时出现异常: {exc}")

    async def on_crash(self, e: Exception) -> None:
        logger.exception(f"MFW 内置运行异常：{e}")
        if self.inner_task is not None and not self._inner_finalized:
            await self.inner_task.on_crash(e)
            return
        await self.script_info.set_state(status="异常")


__all__ = ["MSSManager"]
