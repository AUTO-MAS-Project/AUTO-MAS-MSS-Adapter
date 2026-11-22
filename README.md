<!-- auto-mas:i18n
{"display_name_i18n":{"zh_CN":"MSS 专项","en":"MSS Adapter","ja":"MSS アダプター"},"description_i18n":{"zh_CN":"用 MaaStellaSora 跑《星塔旅人》日常。","en":"Run Stella Sora dailies with MaaStellaSora.","ja":"MaaStellaSora で『ステラソラ』のデイリーを実行します。"},"tags_i18n":{"zh_CN":["星塔旅人","脚本适配"],"en":["Stella Sora","Script adapter"],"ja":["ステラソラ","スクリプト"]}}
-->

# AUTO-MAS MSS Adapter

MSS（MaaStellaSora / 星塔旅人）专项插件：MSS 托管、账号与 MSS 计划表。账号运行沿用宿主迁移分支上 MSS 自己的两层流程
（`manager.MSSManager` → `account_run.MSSAccountRun`），项目更新、运行池、agent 环境、worker 运行等交给 MFW 的三块积木插件
（`maafw_project_update`、`maafw_agent_env`、`maafw_runner`）和两块解析库（`maafw_interface`、`maafw_native`）。

**0.1.0 是联调版**，允许带已知问题发布，清单见下文。

## 依赖与安装

- 宿主：已让出内置 MFW / M9A / MSS 的 AUTO-MAS（联调分支，见开工文档 §5）。宿主仍内置 MSS 时，加载本插件会报
  「当前 AUTO-MAS 仍内置 MFW / M9A / MSS，请换到让位分支」。
- 插件服务：`requires = ("maafw.agent_env.v1", "maafw.runner.v1", "maafw.project.v1")`，即三块积木插件都要先启用。
- 提供服务：`maafw.owner.mss_adapter.v1`（属主入口，项目更新插件按服务名现取）。
- 脚本类型键 `MSS`；只支持自动代理（`AUTO_PROXY`）；计划表类型 `MSSPlanConfig`；活动标识 `stellasora`。

本地联调：把本仓库目录联接到联调宿主树的 `plugins\mss_adapter`，宿主启动时发现并加载（做法见开工文档 §6）。

## 模块

| 模块 | 内容 | 来源（宿主迁移分支 `46785ce81`） |
| --- | --- | --- |
| `plugin.py` | 插件声明、属主登记 | 取代 `app/task/MSS/registration.py` |
| `expander.py` | L3 外壳：整轮交给管理器 | 新写（开工文档 §2.5） |
| `owner.py` | 属主入口、项目拒收原因 | `registration.py` 的属主部分 |
| `config.py` | `MSSConfig`、`MSSUserConfig`、`MSSPlanConfig` | `app/models/config/script.py`、`plan.py` 切片，换 SDK 基类 |
| `manager.py` | 脚本级管理器 | `app/task/MSS/manager.py` |
| `account_run.py` | 账号级流程 | `app/task/MSS/account_run.py` |
| `config_view.py` | 流程读写配置的唯一入口 | `app/task/MSS/config_view.py` |
| `arrange.py`、`rules.py` | 排队列、规则常量与项目判据 | 同名 |

## 已知问题（0.1.0）

**越界导入**（规则 L：只在函数体内首次导入，或落在只在函数体内才被导入的模块里）：

- 直接 import 积木插件的模块（开工文档 §3.2 S1、S2）：`manager.py`、`account_run.py`、`config_view.py` 顶层导入
  `maafw_runner.host.*`、`maafw_runner.core.runner.run_events`、`maafw_project_update.host.*`、
  `maafw_project_update.glue.*`、`maafw_agent_env.host.*`。这三个流程模块只由 `expander.py`、`owner.py` 在函数体内
  首次导入。`owner.build_owner` 在函数体内导入 `maafw_project_update.glue.owner_entry`；`config.MSSUserConfig.update`
  在函数体内导入 `maafw_project_update.glue.user_update`。
- 直接 import 宿主内部模块（SDK 没有公开）：
  - `manager.py` 顶层 `from app.core.notify.lifecycle import NotificationTaskBase`（管理器的基类，挪不进函数体）。
  - 函数体内：`app.core.notify.formats.maafw`、`app.utils.common.process.session.desktop_process_target`、
    `app.tools.common.game_calendar.stella`（`account_run.py`）；`app.task.game.current_game_session`（`manager.py`）；
    `app.task.client`、`app.task.game.game_is_emulator`、`app.utils.common.game_resolution`（`config_view.py`）。
- 其余宿主能力经 SDK 门面 `auto_mas_core.host.*`、`auto_mas_core.devices.task`、`auto_mas_core.adapters.script` 取。

**行为上的简化**（开工文档 §5.2、§8）：

- 执行入口是「L3 外壳 + 沿用的管理器」，L3 的账号循环与 `can_run_user` 不用；L3 收尾可能把脚本状态覆盖成「完成」。
- 宿主不给插件类型绑配置回调：新账号不自动补默认队列（开跑前补）、复制托管不复制视图、删托管不立刻清视图、
  配置备份恢复不可用；保存前加密改由账号类覆盖 `update()` 实现。
- L2 给插件准备的预下载路径没有传给管理器（`after_config_lock` 在插件路径上不挂），预下载过的发行包可能再下一次。
- 「仅更新」模式对插件类型不走插件路径。没有桌面分身。
- 托管与账号载荷从 `UserData` 变成 `users`，多了小写的 `info`；前端是否都读得到没有核实。

## 测试

单元测试只留本地（`tests/` 已写进 `.gitignore`）。

## 发布

由 AUTO-MAS-Project 的 `publish.yml` 使用 `pypi` 环境与同组织的 PyPI Trusted Publisher 完成；只有匹配包版本的发布标签会上传分发包。
