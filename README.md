# Herdr Orchestrator

一个原生 Herdr 插件：把总控 skill、任务协议、持久回报队列、后台轮询和状态窗口统一安装。总控负责派发和汇总，执行代理负责业务操作。

## 安装

需要 **Herdr 0.9.3+、Python 3.11+、Linux 或 macOS**，以及支持原生 `agent_session` 的 Herdr agent integration。无额外 Python 依赖。

```sh
herdr plugin install <owner>/herdr-orchestrator-plugin --ref v0.1.0
```

安装后选择一个已运行的代理窗格，通过 Herdr 插件动作 **Orchestrator: use this agent as coordinator** 指定总控。也可以先明确聚焦该代理，再执行：

```sh
herdr agent focus <代理名或窗格ID>
herdr plugin action invoke start --plugin herdr-orchestrator
```

总控空闲时收到启动消息，读取插件内置 skill 并确认启用。之后直接向总控提出任务。总控按任务领域选择空间，复用或启动 worker；插件自动把接收确认与回报命令加入每次派发，无需另装 CLI 或 worker skill。

```sh
# 打开状态窗口
herdr plugin action invoke status --plugin herdr-orchestrator
# 暂停轮询；保留代理、回报和任务历史
herdr plugin action invoke stop --plugin herdr-orchestrator
```

动作调用返回的是启动记录；可用 `herdr plugin log list --plugin herdr-orchestrator` 检查其真实退出码。首次启动失败且提示缺少 `agent_session` 时，检查 `herdr agent get <目标>` 和对应的 Herdr integration，然后重新执行 start。

## 处理规则

- 每次派发携带目标、范围、授权和验收条件，生成独立 task ID 与 dispatch ID。
- Worker 先明确 `accept`，再执行任务，以 `completed` 或 `blocked` 回报。终端提交成功、代理空闲均不算业务完成。
- 回报正文、消息 ID 和 FIFO 序号在同一 SQLite 事务中保存。相同消息 ID 和内容重试不会重复入队；不同内容复用 ID 会报错。
- 一个 Herdr 会话只有一个当前队首；中断后继续该条。若它产生后续派发，所有后续 worker 明确接收后可出队，**不等待执行完成**。
- 每 5 秒轮询原生状态，仅在总控 idle/done 时尝试唤醒；同一通知最多每分钟重试一次。工具审批阻塞只记录状态，不代答审批。
- 同一原生 worker 会话同时只允许一个未结束的派发。新任务优先复用合适的空闲 worker；同任务补充保持原 task ID 和 worker。
- 暂停后仍可接收已派发 worker 的回报。重新 start 恢复；Herdr 启动钩子恢复已启用的绑定。代理身份变化需暂停后显式重新绑定。

总控收到的启动消息包含完整的插件入口命令（下文记作 `CLI`），带有绝对程序路径、状态目录和会话 socket。其主要命令为：

```text
CLI ready
CLI dispatch --target WORKER --task-file TASK.json
CLI wait-accepted --dispatch DISPATCH_ID --timeout 10
CLI next
CLI dispatch --target WORKER --task-file TASK.json --task-id TASK_ID --parent-event SEQ
CLI finish --event SEQ --summary-file SUMMARY.txt
CLI status
CLI stop
CLI bind --target COORDINATOR --rebind
```

任务文件：

```json
{
  "goal": "具体要完成的目标",
  "scope": "允许操作的项目、文件或环境",
  "authorization": "已获授权的动作与边界",
  "acceptance_criteria": "完成标准与所需验证"
}
```

Worker 命令由派发消息提供。结果文件必须是绝对路径的 UTF-8 文件，插件同时保存路径和正文。总控可直接读取 `next` 返回的持久化正文，不依赖源文件仍存在。检查后撤销派发可用 `CLI abandon --dispatch ID --reason 原因`；撤销不会终止执行代理，应先核实其实际状态和后续处置。

## 数据与边界

状态保存在 Herdr 提供的 `HERDR_PLUGIN_STATE_DIR`，包括 `state.sqlite3`、WAL 文件以及每个会话的锁与日志。任务授权、结果和摘要均留在本机，不上传到 GitHub。卸载前需要的数据应单独备份；插件不自动清理历史记录。

队列顺序指 SQLite 成功写入的先后顺序。数据库约束保证唯一队首和接收后出队；总控职责、领域路由和业务正确性仍由 skill 与代理执行。插件不是代理权限沙箱。

Herdr 目前没有原子的「仍然空闲才提交」条件接口；读取状态与终端提交之间存在竞争。终端投递和数据库也不能共享事务，因此不承诺业务动作恰好执行一次。投递失败或超时会保留派发 ID 并明确报错，先查记录和 worker，再决定是否撤销或继续，程序不会自动重派业务任务。

已启用绑定依赖同一原生代理会话身份。如果总控被替换，程序拒绝把旧任务自动交给新代理；暂停后用完整入口命令显式 `bind --target ... --rebind`。移动未命名代理到其他空间后，窗格 ID 会变化，需检查绑定或重新绑定。总控忙时唤醒会推迟；宿主退出期间不会投递消息。

## 开发与验证

```sh
python3 orchestrator.py check-runtime
python3 -m unittest discover -s tests -v
herdr plugin link "$PWD"
```

测试覆盖 FIFO 重入、并发写入、接收门槛、幂等回报、身份隔离、代理复用、暂停恢复及不明投递。真实 Herdr 0.9.3 集成验证使用独立测试会话与模拟终端执行代理，检查原生动作、后台唤醒、终端投递、接收、回报、状态 popup 和暂停；不代表真实模型自动完成业务任务已验证。macOS 尚未实机验证。

协议与接口依据：[Herdr Socket API](https://herdr.dev/docs/socket-api/)。

MIT License。
