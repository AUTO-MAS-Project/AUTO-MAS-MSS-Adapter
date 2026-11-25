<!-- auto-mas:i18n
{"display_name_i18n":{"zh_CN":"MSS 专项","en":"MSS Adapter","ja":"MSS アダプター"},"description_i18n":{"zh_CN":"用 MaaStellaSora 跑《星塔旅人》日常。","en":"Run Stella Sora dailies with MaaStellaSora.","ja":"MaaStellaSora で『ステラソラ』のデイリーを実行します。"},"tags_i18n":{"zh_CN":["星塔旅人","脚本适配"],"en":["Stella Sora","Script adapter"],"ja":["ステラソラ","スクリプト"]}}
-->

# AUTO-MAS MSS Adapter

MSS（MaaStellaSora / 星塔旅人）专项插件：MSS 托管、账号与 MSS 计划表。账号运行沿用宿主迁移分支上 MSS 自己的两层流程
（`manager.MSSManager` → `account_run.MSSAccountRun`），项目更新、运行池、agent 环境、worker 运行等交给 MFW 的三块积木插件
（`maafw_project_update`、`maafw_agent_env`、`maafw_runner`，只经它们的服务调，不 import 它们的模块），判定、记账、文案等
取专项共用库（`maafw_adapter_kit`）和两块解析库（`maafw_interface`、`maafw_native`）。

**0.1.0 是联调版**，允许带已知问题发布，清单见下文。

## 依赖与安装

- 宿主：已让出内置 MFW / M9A / MSS 的 AUTO-MAS（联调分支，见开工文档 §5）。宿主仍内置 MSS 时，加载本插件会报
  「当前 AUTO-MAS 仍内置 MFW / M9A / MSS，请换到让位分支」。
- 插件服务：`requires` 按 MFW 服务化方案写全三个专项共有的 70 个服务操作名（专项共用库的常量拼成），三块积木插件
  都要先启用。经服务调的都经 `services.SLOT`（插件启用时绑宿主给的解析器、停用时解，管理器、账号流程、钩子从槽里现取）。
- 提供服务：属主服务 `maafw.owner.mss_adapter.v1.<操作>` 的 8 个操作（专项共用库的 `ConfigOwner` + `OwnerServiceMixin`），
  启用时经项目服务 `maafw.project.v1.owner_register` 登记、停用时 `owner_unregister`（生命周期钩子里只试一次）。
- 发行包依赖：两块库写 `>=0.3.0,<0.4`，共用库写 `>=0.3.1,<0.4`，对三块服务插件只写下限 `>=0.3.0`。
- 脚本类型键 `MSS`；只支持自动代理（`AUTO_PROXY`）；计划表类型 `MSSPlanConfig`；活动标识 `stellasora`。

本地联调：把本仓库目录联接到联调宿主树的 `plugins\mss_adapter`，宿主启动时发现并加载（做法见开工文档 §6）。

## 模块

| 模块 | 内容 | 来源（宿主迁移分支 `46785ce81`） |
| --- | --- | --- |
| `plugin.py` | 插件声明、属主登记、托管生命周期钩子、停用预检 | 取代 `app/task/MSS/registration.py` |
| `hooks.py` | 托管生命周期钩子与按配置的判定的实现 | 宿主 `MaaFW/seams/config_handler.py`、`script_lifecycle.py` |
| `expander.py` | L3 外壳：整轮交给管理器 | 新写（开工文档 §2.5） |
| `owner.py` | 属主（新形 `ConfigOwner`，模块级 `OWNER`）、项目拒收原因 | `registration.py` 的属主部分 |
| `services.py` | 服务槽 `SLOT`、`requires`、经槽现取服务的客户端 | 新写（MFW 服务化） |
| `config.py` | `MSSConfig`、`MSSUserConfig`、`MSSPlanConfig` | `app/models/config/script.py`、`plan.py` 切片，换 SDK 基类 |
| `manager.py` | 脚本级管理器 | `app/task/MSS/manager.py` |
| `account_run.py` | 账号级流程 | `app/task/MSS/account_run.py` |
| `config_view.py` | 流程读写配置的唯一入口 | `app/task/MSS/config_view.py` |
| `arrange.py`、`rules.py` | 排队列、规则常量与项目判据 | 同名 |

## 托管生命周期与宿主出口（0.2 起）

要宿主带 SDK 的托管生命周期钩子、`ScriptTypeDecl.game_kind` / `desktop_session`（迁移分支合并 SDK 三组之后）；
旧宿主上插件拒绝加载（「当前 AUTO-MAS 版本过旧……」）。这几项出口没有单独抬 SDK 版本号，所以 `auto-mas-core`
的下限没法写出区别，靠导入时检查。

| 钩子 / 声明 | 做什么 |
| --- | --- |
| `prepare_user_update` | 保存账号前：全局选项双写、任务快照里的密码字段加密（专项共用库 `user_update`） |
| `prepare_user_read`、`on_user_added` | 读账号前、新账号提交后：空任务队列按项目默认写成实打实的队列（项目没导入时留到下次） |
| `on_script_cloned` | 复制托管：关掉目标的开发者模式，按来源载荷建目标视图并记下属主 `mss_adapter` |
| `on_script_removed` | 删除托管：当场删掉这个托管的视图（记着别的属主时不删），再起一轮运行池对账（项目服务 `script_removed`） |
| `decl.game_kind` | 所选控制器是 Adb 时只能绑模拟器，其余只能绑客户端；判断不了时不收窄 |
| `decl.desktop_session` | 运行时会选的控制器是 Win32 才能进桌面分身 |
| `teardown_blockers` | 还有 MSS 托管在跑时拒绝停用、重载、卸载 |

外壳把管理器记下的「跳过」（项目正在切换 / 更新 / 被占用，或全员因停服维护跳过）和运行前检查没过的「异常」交给
L3 收尾，不再被覆盖成「完成」；管理器回调出错时也记「异常」。外壳接宿主的 `on_stop_requested`（用户停止时转给管理器的
`_on_finalize_cancel_requested`），声明 `defer_game_session`（管理器确认有可运行账号之后才选定游戏会话）；宿主没有这两个
钩子时照旧。

## 跨插件调用（MFW 服务化）

源码里不 import 三块服务插件的任何模块（含函数体内）。经服务调的全部经 `services.SLOT` 现取：

- agent 环境：管理器的运行环境自检、运行池路由（`runtime_check`、`runtime_route`）。
- 项目：打开项目（共用库 `open_project` → `open_*`）、运行前 / 后更新（`update_*`）、运行环境确认（`confirm_*`）、收尾项目
  （`finish_*`）、队列预下载的发现（`discover_*`）、项目口径（`project_context`）、账号预约（`run_reserve`，交回经共用库
  `release_run_reservation` → `run_release`）、资源包修复（`repair_*`）、复制 / 删除托管（`script_cloned_*`、`script_removed_*`）。
  请求里的托管记录经本插件的属主 `owner.OWNER` 现读（共用库 `record_payload` 的字典形），导入报告也经它写回。
- 运行：账号句柄（`account_open` / `account_close`）、桌面游戏（`desktop_check` 在建计划的工作线程里同步调、`desktop_launch_*`、
  `desktop_quit_*`）、设备配置（`device_*`）、运行前设置（`pretask_*`、`pretask_rerun_check`、`pretask_kill_*`，停止后的退出确认
  `pretask_exit_watch_*` 由共用库的交回预约助手调）、跑计划（`plan_*`）。

## 已知问题

- 项目更新的 `update_start` 契约不收这一轮开始的时刻 `round_started_at`，运行前更新的会话请求里这一项是 0。项目更新只在收尾
  （`finish_*`，带着它）用这个时刻，行为不变；积木仓补上这个可选字段后，`manager._run_project_update` 要跟着传。
- 删除托管时，视图记着本属主、却是别的托管 id（视图目录名只取 uuid 前 12 位、撞上时共用）的情形不再单独认出来保留：项目服务的
  `script_removed` 只按属主判断，也不回记着的托管 id。

**越界导入**：

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
- 预下载：L2 把交付的路径写进任务参数 `prefetch_paths`，管理器组运行前更新（`update_*`）的请求时照旧从参数里取（读代码
  推断，没实跑）。
## 测试

单元测试只留本地（`tests/` 已写进 `.gitignore`）。

## 发布

由 AUTO-MAS-Project 的 `publish.yml` 使用 `pypi` 环境与同组织的 PyPI Trusted Publisher 完成；只有匹配包版本的发布标签会上传分发包。
