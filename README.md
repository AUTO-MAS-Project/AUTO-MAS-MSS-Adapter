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
| `plugin.py` | 插件声明、属主登记、托管生命周期钩子、停用预检 | 取代 `app/task/MSS/registration.py` |
| `hooks.py` | 托管生命周期钩子与按配置的判定的实现 | 宿主 `MaaFW/seams/config_handler.py`、`script_lifecycle.py` |
| `expander.py` | L3 外壳：整轮交给管理器 | 新写（开工文档 §2.5） |
| `owner.py` | 属主入口、项目拒收原因 | `registration.py` 的属主部分 |
| `config.py` | `MSSConfig`、`MSSUserConfig`、`MSSPlanConfig` | `app/models/config/script.py`、`plan.py` 切片，换 SDK 基类 |
| `manager.py` | 脚本级管理器 | `app/task/MSS/manager.py` |
| `account_run.py` | 账号级流程 | `app/task/MSS/account_run.py` |
| `config_view.py` | 流程读写配置的唯一入口 | `app/task/MSS/config_view.py` |
| `arrange.py`、`rules.py` | 排队列、规则常量与项目判据 | 同名 |

## 托管生命周期与宿主出口（未发布的 0.2 起）

要宿主带 SDK 的托管生命周期钩子、`ScriptTypeDecl.game_kind` / `desktop_session`（迁移分支合并 SDK 三组之后）；
旧宿主上插件拒绝加载（「当前 AUTO-MAS 版本过旧……」）。这几项出口没有单独抬 SDK 版本号，所以 `auto-mas-core`
的下限没法写出区别，靠导入时检查。

| 钩子 / 声明 | 做什么 |
| --- | --- |
| `prepare_user_update` | 保存账号前：全局选项双写、任务快照里的密码字段加密（专项共用库 `user_update`） |
| `prepare_user_read`、`on_user_added` | 读账号前、新账号提交后：空任务队列按项目默认写成实打实的队列（项目没导入时留到下次） |
| `on_script_cloned` | 复制托管：关掉目标的开发者模式，按来源载荷建目标视图并记下属主 `mss_adapter` |
| `on_script_removed` | 删除托管：当场删掉这个托管的视图（记着别的属主或别的托管时不删），再起一轮运行池对账 |
| `decl.game_kind` | 所选控制器是 Adb 时只能绑模拟器，其余只能绑客户端；判断不了时不收窄 |
| `decl.desktop_session` | 运行时会选的控制器是 Win32 才能进桌面分身 |
| `teardown_blockers` | 还有 MSS 托管在跑时拒绝停用、重载、卸载 |

外壳把管理器记下的「跳过」（项目正在切换 / 更新 / 被占用，或全员因停服维护跳过）和运行前检查没过的「异常」交给
L3 收尾，不再被覆盖成「完成」；管理器回调出错时也记「异常」。

## 已知问题

**越界导入**（规则 L：只在函数体内首次导入，或落在只在函数体内才被导入的模块里）：

- 直接 import 积木插件的模块（开工文档 §3.2 S1、S2）：`manager.py`、`account_run.py`、`config_view.py` 顶层导入
  `maafw_runner.host.*`、`maafw_runner.core.runner.run_events`、`maafw_project_update.host.*`、`maafw_agent_env.host.*`。
  这三个流程模块只由 `expander.py`、`owner.py` 在函数体内首次导入；`hooks.py` 里用到的项目更新模块都在函数体内导入。
  专项共用代码改从库 `maafw_adapter_kit` 取（顶层只依赖解析库）。
- 直接 import 宿主内部模块（SDK 还没有出口）：`manager.py` 顶层 `app.core.notify.lifecycle.NotificationTaskBase`（管理器的
  基类，挪不进函数体）；`account_run.py` 函数体内 `app.core.notify.formats.maafw`。其余宿主能力（客户端绑定、游戏目标、
  分身进程目标、星塔日历、Unity 分辨率等）都经 SDK 门面 `auto_mas_core.host.*`、`auto_mas_core.devices.task`、
  `auto_mas_core.adapters.script` 取。

**行为上的简化**（开工文档 §5.2、§8）：

- 执行入口仍是「L3 外壳 + 沿用的管理器」，L3 的账号循环与 `can_run_user` 不用。
- 配置备份恢复不可用（恢复池与路由没搬，宿主 `seams/restore_service` 那一套要按 SDK 的恢复基座重写）；账号页「打开配置
  目录」没有 `info.config_path`。
- 「仅更新」模式对插件类型不走插件路径。
- 托管与账号载荷从 `UserData` 变成 `users`，多了小写的 `info`；前端是否都读得到没有核实。
- 预下载：L2 把交付的路径写进任务参数 `prefetch_paths`，管理器建项目会话请求时照旧从参数里取（读代码推断，没实跑）。
## 测试

单元测试只留本地（`tests/` 已写进 `.gitignore`）。

## 发布

由 AUTO-MAS-Project 的 `publish.yml` 使用 `pypi` 环境与同组织的 PyPI Trusted Publisher 完成；只有匹配包版本的发布标签会上传分发包。
