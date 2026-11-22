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


class MSSExpander(UserExpander):
    """0.1.0：整轮交给沿用的脚本级管理器；L3 只做外壳。"""

    async def main_task(self) -> None:
        await self.script_info.set_state(status="运行")
        self._entered = True
        from .manager import MSSManager  # 规则 L：函数内导入

        manager = MSSManager(self.script_info, host_locked_config=self.script_config)
        manager.history = self.history
        manager.context = self.context
        manager.maintenance = False
        # L2 收尾时复位（app/task/script_expander.py 的 finally）
        self.script_info._manager_controls_users = True
        try:
            await manager._execute_task()
            error = manager.execution.error
            if error is not None and type(error).__name__ == "FluxdownCancelUnconfirmed":
                raise error
        finally:
            await manager.callbacks.close()
            if self.task_info.mode == TaskMode.AUTO_PROXY:
                manager.finalize_completion_details(self.script_info)

    def build_worker(self, user: Any, config: Any) -> Any:
        raise NotImplementedError("0.1.0 由 main_task 整轮交给管理器")


async def prefetch_check(config: Any) -> list:
    """预下载检查：交给管理器的静态 ``check_prefetch``（现导入，规则 L）。"""

    from .manager import MSSManager

    return await MSSManager.check_prefetch(config)


__all__ = ["MSSExpander", "prefetch_check"]
