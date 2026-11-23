#   AUTO-MAS: A Multi-Script, Multi-Config Management and Automation Software
#   Copyright © 2025-2026 AUTO-MAS Team
#   SPDX-License-Identifier: AGPL-3.0-or-later

"""MSS 托管的执行入口（L3 外壳）。

0.1.0 不重写执行树：L2 拿到托管 X 锁、建好本外壳后，``main_task`` 把整轮交给沿用的脚本级管理器
``manager.MSSManager``（构造时告诉它宿主已持锁）。旧路径里 L2 给管理器的 ``history`` / ``context`` /
``maintenance`` / ``_manager_controls_users`` 在这里补上（宿主 ``app/task/script_expander.py`` 的旧工厂分支）。
"""

from __future__ import annotations

from typing import Any

from auto_mas_core import TaskMode, UserExpander

# 正在跑的外壳（插件的 teardown_blockers 据此拒绝停用、重载、卸载）
_RUNNING: set[int] = set()

#: 宿主有没有「确认有账号之后再选定游戏」这个钩子（旧宿主上 L2 仍在派发 L3 之前选定）
_DEFERRED_GAME_SESSION = hasattr(UserExpander, "select_game_session")


def running_count() -> int:
    """正在跑的 MSS 托管数。"""

    return len(_RUNNING)


class MSSExpander(UserExpander):
    """整轮交给沿用的脚本级管理器；L3 只做外壳。

    管理器自己记托管状态，L3 收尾（``UserExpander.final_task``）只认「异常 / 跳过 / 完成」三档：管理器记了
    「跳过」（项目正在切换、更新或被占用，或者全员因停服维护跳过）时这里记下 ``_skipped``，管理器记了「异常」
    而账号里没有异常（运行前检查没过）、或者管理器的回调出了错时记下 ``error``，免得收尾覆盖成「完成」。
    """

    #: 由管理器在确认有可运行账号之后选定游戏会话（原来 after_config_lock 那个位置）：没有可运行
    #: 账号就不占游戏、不排队
    defer_game_session = True

    _manager: Any = None

    def on_stop_requested(self) -> None:
        """L2 送出取消之前同步通知：转给管理器（收尾里的运行后更新、环境确认按停止处理）。"""

        manager = self._manager
        if manager is not None:
            manager.stopped_manually = True
            manager._on_finalize_cancel_requested()

    async def main_task(self) -> None:
        await self.script_info.set_state(status="运行")
        self._entered = True
        from .manager import MSSManager  # 规则 L：函数内导入

        manager = MSSManager(self.script_info, host_locked_config=self.script_config)
        manager.history = self.history
        manager.context = self.context
        manager.maintenance = False
        if _DEFERRED_GAME_SESSION:
            # 拿到托管锁、确认有可运行账号之后由管理器调（同旧专项 lock_script_config 的约定）
            manager.after_config_lock = self.select_game_session
        self._manager = manager
        # L2 收尾时复位（app/task/script_expander.py 的 finally）
        self.script_info._manager_controls_users = True
        _RUNNING.add(id(self))
        try:
            await manager._execute_task()
            error = manager.execution.error
            if (
                error is not None
                and type(error).__name__ == "FluxdownCancelUnconfirmed"
            ):
                raise error
        finally:
            _RUNNING.discard(id(self))
            self._manager = None
            await manager.callbacks.close()
            if self.task_info.mode == TaskMode.AUTO_PROXY:
                manager.finalize_completion_details(self.script_info)
        self._adopt_manager_outcome(manager)

    def _adopt_manager_outcome(self, manager: Any) -> None:
        """把管理器记下的托管结论交给 L3 收尾（见类说明）。"""

        status = self.script_info.status
        if status == "跳过":
            self._skipped = str(manager.check_result or "跳过")
        elif status == "异常" and self.error is None:
            if manager.check_result not in ("Pass", "-"):
                self.error = RuntimeError(manager.check_result)
        if self.error is None and manager.callbacks.errors:
            # 旧路径上 L2 据此把整个任务记成失败（_callback_failed）
            self.error = RuntimeError("MSS 托管运行中的回调出错")

    def build_worker(self, user: Any, config: Any) -> Any:
        raise NotImplementedError("0.1.0 由 main_task 整轮交给管理器")


async def prefetch_check(config: Any) -> list:
    """预下载检查：交给管理器的静态 ``check_prefetch``（现导入，规则 L）。"""

    from .manager import MSSManager

    return await MSSManager.check_prefetch(config)


__all__ = ["MSSExpander", "prefetch_check"]
