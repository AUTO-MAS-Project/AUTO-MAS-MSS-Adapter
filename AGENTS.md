# app/task/MSS — MSS 专项：自己的账号流程，用 MFW 的积木

> 迁移前的宿主约束：本文从宿主迁移分支 `46785ce81` 的 `app/task/MSS/AGENTS.md` 搬来，删掉了风味路径与回退两节。
> 文中的 `app/task/MaaFW/...`、`app/task/MSS/...` 路径指宿主旧位置；插件版的对应关系见 README。

进这个目录之前先读 `app/task/MaaFW/AGENTS.md`：MSS（MaaStellaSora / 星塔旅人）的项目更新、内嵌副本、运行池、
agent 环境、worker 运行、结果判定、通知正文、失败截图、周期任务、代理次数……都是 MFW 积木的既有语义，MSS 只调、
不重写。

## 自己的流程（MFW 插件化迁移第 10 步起，总方案 §3.7）

MSS 托管不再经 MFW 管理器和风味钩子运行，而是走本目录自己的两层流程。**账号运行的流程三个专项（通用 MFW、
M9A、MSS）各写一份**，互不继承、互不导入（MSS 不导入 `app.task.M9A` 的任何模块，结构与 M9A 相同的地方也各写
一份）；共用的只有 `app/task/MaaFW/tools/` 下吃普通数据的叶子（`run_settings`、`run_messages`、`run_notices`、
`run_redaction`、`attempt_ledger`、`host_client`、`desktop_game`、`project_environment`、`resource_repair`、
`project_path.release_run_reservation`、`task_selection.build_arranged_plan`、`config_handler` 等）。

- `registration.py`：按确切类型 `MSSConfig` 登记。`manager_factory` → `manager.MSSManager`（在函数里现导入，
  启动期只导入登记本身）；配置回调与恢复池**仍是通用的**（`build_maafw_config_handler(MSSConfig, owner=MSS_OWNER)`、
  `load_maafw_restore_pools`，取自中立模块 `MaaFW/tools/embedded/config_handler`）：MSS 没有保存前整理，风味路径上
  那一步对它本来就是空操作。另有托管属主 `mss_adapter`（`register_owner()`）。
- `manager.py`：`MSSManager`，脚本级编排（运行前检查：类型闸门只认 `MSSConfig`、架构自检、项目会话
  reserve / hold、X 锁、筛账号、模拟器、运行时自检；`main_task`：运行前更新、遥测 `script_type="MSS"`、逐个账号；
  `final_task`：代理结果、信号通知、统计）。带**静态 `check_prefetch`**（`prefetch.PrefetchSession._check` 用
  `getattr` 读，漏了会静默失效），构造函数没有副作用。项目会话请求按 MSS 属主 `read_managed` 的记录组。
- `account_run.py`：`MSSAccountRun`，账号级编排（`check` → `main_task` 的重试循环 → `final_task`）。MSS 原来靠
  风味钩子做的事在这里是普通代码：
  - **建计划分三段**，整段是一个协程 `_load_run_state`，由 `check` 用原来那个 shield 循环等（用户这时点停止，照样
    等这一整段做完、不越过 X 锁释放）：工作线程里 `_plan_head`（读 interface、用空规则表 `EMPTY_PLAN_RULES` 展开
    快照、`config_view.plan_slot` 取计划表今天那一格、`arrange.mss_arrange_head`）→ 回到事件循环，**只在
    `probe_needed` 时** `await stella.has_running_official_activity()`（在函数里现导入、按模块属性调；每次运行最多
    一次；抛异常时包成 `MaaFWRunPlanError(describe_plan_failure(...))`，与旧路径同一文案）→ 工作线程里
    `_plan_tail`（`mss_arrange_tail`、`build_arranged_plan`、周期过滤、桌面分身与 Win32 检查）。两段工作线程的账号
    日志都经 `send_plan_log`（`call_soon_threadsafe`）回到事件循环，先后与一次排完时一字不差。
  - **活动探测在事件循环上等**是与旧的风味路径（工作线程里 `asyncio.run`）唯一的差别，其余产物新旧必须完全相同
    （实施方案 §12 第 2 条；对拍单独断言线程）。
  - 没有运行前登录、游戏更新、交给首任务的登录：ADB 照样能跑（引擎不拦 Adb；第 11 步之后要不要拒绝待用户拍板，默认保留），
    `ask_toolkit` 恒为假、设备配置不插步骤；Win32 直接找窗口。
  - 重试剔除只在「失败、不修资源、要重试」那条分支里做，名单是 `rules.MSS_SKIP_ON_RETRY`。
- `config_view.py`：流程读写配置的**唯一入口**，只给函数、返回普通值，每次现读，写字段一律「改一项、提交一次」。
  `account_run`、`manager` 不直接读配置属性（`.Info` / `.Run` / `.UserData` / `related_config` ……），吃配置对象的
  宿主函数也只在这里调（`close_emulator(self)` 传的是任务对象，例外）。MSS 自己的两项也在这里：`plan_slot`
  （照旧经 `type(user).related_config["PlanConfig"][UUID(mode)]` 取计划表，被删、类型不对时写那行
  「[MSS] 引用的计划表不可用，本轮不改悬赏试炼：…」；插件化时只改这一处）与 `if_activity_first_raw`。

**这个流程自己负责的事**（不再交给 MFW 管理器）：账号能不能跑与成功后扣剩余天数；重试上限、资源修复后的重跑不计
次数、重试前只关自己开的；代理次数、最近日期与状态的结算；每次尝试单独的日志、历史、成功通知、统计、脚本级汇总；
运行前归档；显示保持、锁屏检测、静默模式；停服维护连带跳过与合并通知；原生库与架构检查；脚本前后任务、遥测；
两组打码值；以及上面那几条 MSS 自己的规矩（计划表、活动、爬塔、重试剔除）。

**边界**（本地守卫 `tests/task/test_mss_flow_guard.py`、`test_mss_registration_guard.py`）：
- 本目录（`flavor.py` 除外）不导入 `app.task.MaaFW.embedded_manager`、`tools/embedded/runner_task`、
  `tools/embedded/flavor`、`flavor_support`、`app.task.MaaFW.registration`、`app.task.M9A`；`registration.py` 可以导入
  `app.task.MaaFW.owner_entry`（共用的宿主胶水）。活动探测在 `account_run` 里只有一处 `await stella.…()`。
- 不抽带钩子、回调插槽或抽象方法的共用主循环，不造「账号运行上下文」大对象：重试循环、`check` 的步骤先后、
  `final_task` 的 finally 链、设备配置的拼装顺序、请求组装都在本目录写一份（`_PlanHead` 只是建计划前后两段之间
  传的一小份普通数据，不跨出 `_load_run_state`）。
- 照旧路径**原样保留**、不要顺手改的：logger 名「MaaFW 插件自动代理」/「MFW 内置运行」；`lease_owner` 仍是
  `automas-script-maafw:{id}`；管理器直接 `try_lock_x()`、不走 `lock_script_config`；与通用流程相同的两处既有问题
  （脚本后任务抛异常时 `on_crash` 被调两次、代理结果写「全部完成」而账号状态是「异常」；资源包核对任务不在作用域的
  保留名单里，停止时会被取消）——要修就三个专项一起修，不在一份里单独改；按任务名比 `excluded_task_ids`（实例 id
  的集合），第二个活动实例设成红灯挡不住自动补入，同样是既有口径。
- 游戏会话（上游 #2107、#2108 合进来后，与通用 MFW 逐行相同）：`_check` 确认有可运行账号之后、取模拟器之前调 L2 挂的
  `after_config_lock`（选定托管级游戏会话、复查预下载），失败时交回托管锁、把原因作为检查结果；账号循环里每个账号
  先 `_select_account_game` 按账号选定（选定失败只把这个账号记异常），整个账号包在 `try/finally` 里、`finally` 对
  会话 `release_account()`。活动探测在账号流程的 `check` 里，在按账号选定之后。判「另绑模拟器没选设备」的读取在
  `config_view.account_emulator_without_device`。
- 管理器每次经 MSS 属主 `read_managed` 现取托管记录（旧路径用手里的配置对象），读不到抛 `_ManagedRecordMissing`：
  只有「保存账号配置并解锁之后、收尾期间，用户恰好删了这个托管」这一窄窗口里两边不同——收尾项目那一步在 `try` 里，
  记一行「MFW 收尾维护失败」后跳过；补确认运行环境那一步记一行 warning 后跳过、不把结果改成异常（实施方案 §13.2，
  对拍里是 S35 的允许差异）；旧路径会拿已删托管的旧对象照常收尾。运行阶段读不到照旧向上抛。与 M9A 同一口径。
- 启动期导入链：`import app.task.MSS.registration` 不拉起 `account_run`、`manager`、`config_view`、风味模块与
  `MaaFW/registration`。

**合并上游时要手工同步的两个文件**：第 10 步到第 11 步之间，同一份逻辑在通用流程、M9A、MSS 各有一份。合并上游
dev 时，凡是改了 `app/task/MaaFW/tools/embedded/runner_task.py` 的，要把同样的改动搬进本目录的 `account_run.py`
（和 M9A 的那份）；改了 `app/task/MaaFW/embedded_manager.py` 的，搬进 `manager.py`（同上）。建计划那段（`runner_task`
的 `_load_run_state_for_check` / `_build_run_plan`）在本目录拆成了 `_load_run_state` / `_plan_head` / `_plan_tail`，
改动按所在的段落搬。改动落在 `tools/` 叶子上的（例如 #2107 的预下载提前返回在 `project_session.discover_prefetch`）
三份都调它，不用搬。合并后先跑实时对拍，它会报出分叉；合并说明里逐个列出同步了哪些函数。金样怎么跟着合并重录见
`app/task/MaaFW/AGENTS.md`「从现在到第 11 步之间合并上游」。

**对拍**（迁移期间的本地测试，不随 PR 提交；详见 `tests/task/mfw_parity/README.md`）：从树根用本树解释器跑
`python -m pytest tests/task/mfw_parity -p no:cacheprovider --basetemp <临时目录下的新目录> -q`（全量前先设好
`AUTO_MAS_UV_EXE`）。金样经宿主的登记取管理器，翻登记后 S 组守的就是本目录的流程（探测线程按上面那条唯一差异
单独断言），金样只许在旧路径上重录（没合并过上游时是基线 `3bf58bb13` 的导出目录，合并过之后是最近一个合并提交的
导出目录里反向应用两个翻登记提交，实施方案 §13.1）；实时对拍旧路径显式用 MFW 管理器 + 风味路径。本目录的新
模块引用了宿主函数时，把模块路径加进 `harness.NEW_FLOW_MODULES`。

## 规则与排队列

- `rules.py`：三个 entry、`MSS_SKIP_ON_RETRY`（上面第 3 条的名单就是它）、`TYPE_KEY`、`PLAN_MODE_FIXED`、
  `is_mss_project` / `github_repo_name`，全包只有这一份；只依赖解析一侧。
- `arrange.py`：排队列拆成活动探测前后两段，只吃 interface、队列与调用方取好的值（计划表当天那一格、
  `Info.IfActivityFirst` 原值、排除的实例），不读配置。`mss_arrange_head` 套计划表并算出要不要查活动排期
  （`probe_needed`：活动不在队列里、没被排除、开着活动优先 = `want_add`；活动在队列里 = `want_judge`；
  两者互斥），`mss_arrange_tail` 按查到的结果补上 / 挪动 / 摘掉活动，再把爬塔挪到队尾。
  `flavor.decorate_selection` = 取计划表那一格（`_current_plan_key`，日志在这一步写）→ head →
  只在 `probe_needed` 时调一次探测 → tail。**判断条件在原来的位置算出来，最多探一次，日志先后与原来一次
  排完时一字不差**；改这两段时守住这三条（本地对拍 S1–S29 与探测次数矩阵 S16 守着）。`account_run._load_run_state`
  照同样的先后调它们，只是探测改在事件循环上等。

配置类在 `app/models/config/script.py`（计划表类在 `app/models/config/plan.py`）：`MSSConfig(MaaFWConfig)` 同形，只改 `DEFAULT_SCRIPT_NAME`、
`USER_CONFIG_CLASS`、`FLAVOR`；`MSSUserConfig(MaaFWUserConfig)` 多一项 `Info.PlanMode`（计划表消费方
`mss`，`PLAN_BOOK` 的 `MSSPlanConfig`）与 `Info.IfActivityFirst`（活动优先）。
本目录的 `registration.py` 按确切类型 `MSSConfig` 登记运行（第 10 步起是自己的 `MSSManager`，见上文）、配置回调与
恢复池（与通用 MFW 同一套，从中立模块 `MaaFW/tools/embedded/config_handler` 取）以及托管属主 `mss_adapter`（见
`app/task/MaaFW/AGENTS.md`「托管属主登记」）；宿主按确切类型查，不靠继承。

前端托管页 / 账号页是 MSS 的专属页面（`frontend/src/views/mss/`：`MSSScriptEdit.vue`、`MSSUserEdit.vue`，
用 MaaFW 的 pageKit 分节与编排层拼），由同目录的 `entry.ts` 登记（适配条目上的 `maafw` 家族标记，计划表也在这里登记）；
托管页控制方式一步顶部「只支持桌面端」的提示、
账号页的计划表下拉与活动优先开关都是同目录下的组件。计划表页 `MSSPlanTable.vue` 是 MSS 自己的。

只适配桌面端：模拟器端的星塔旅人启动不了游戏，托管页在控制方式一步写明原因；引擎不拦 Adb。

要给 MSS 加任何"专项行为"之前先问：这是不是所有 MaaFW 项目都该有的？是就做进引擎（`app/task/MaaFW/tools/` 下吃
普通数据的叶子，三个专项的流程都调）；不是就写进本目录自己的流程（第 11 步之前，回退本体 `flavor.py` 要不要同步
按回退时要不要有这个行为定），`app/task/MaaFW/` 里不出现 MSS 逻辑。
