# Herdr Orchestrator

一个原生 Herdr 插件：把总控 skill、任务协议、持久回报队列、后台轮询和状态窗口统一安装。总控负责派发和逐条汇总，执行代理负责业务操作。多个回报同时到达时，只展示当前一条；用户处理完并明确确认后，才主动通知下一条。

## 安装

需要 **Herdr 0.9.3+、Python 3.11+、Linux 或 macOS**，以及支持原生 `agent_session` 的 Herdr agent integration。无额外 Python 依赖。

```sh
herdr --version
python3 --version
herdr plugin install <owner>/herdr-orchestrator-plugin --ref v0.2.0
# 确认 version、enabled 和 GitHub source
herdr plugin list --plugin herdr-orchestrator --json
```

安装命令会展示清单并请求确认；需要非交互安装时加 `--yes`。检查输出中的 `enabled` 为 `true`；已禁用时执行 `herdr plugin enable herdr-orchestrator`。

Codex 用户还需要 Herdr 的原生 integration：

```sh
herdr integration status
# 仅在 codex 显示未安装或过期时执行
herdr integration install codex
```

安装 integration 后新启动 Codex，确保新会话加载它。在 Herdr 的空闲 shell 窗格内运行 `codex --no-daemon`，先发送一条普通消息，再确认 `herdr agent get <目标>` 含有 `agent_session.value`。`--yolo` 会关闭 Codex 审批与沙箱；根据自己的任务授权选择是否使用，插件本身不会更改这些设置。如果代理工具读不到 Herdr 环境变量，重新用 `--no-daemon` 启动并核对。

无需 `pip install`，也无需把 skill 另外复制进 Codex 技能目录。

### 从 v0.1.0 升级并保留历史

先暂停使用此插件的会话编排，备份原 `HERDR_PLUGIN_STATE_DIR` 中的 SQLite 数据库。使用 SQLite backup 生成一致备份；如果直接复制目录，应先停止数据库写入并连同 WAL 保存，不能只复制正在使用的 `state.sqlite3`。

Herdr 0.9.3 的插件 CLI 没有 update 子命令。GitHub 安装版更换版本时，保留原状态目录和备份，再卸载旧插件并安装上面的 v0.2.0：

```sh
herdr plugin uninstall herdr-orchestrator
herdr plugin install <owner>/herdr-orchestrator-plugin --ref v0.2.0
herdr plugin list --plugin herdr-orchestrator --json
```

核对实际版本为 0.2.0、原状态目录仍在，然后在原总控会话执行 start 恢复。若安装工具清理了状态目录，先恢复一致备份再恢复编排。插件首次打开原数据库时，在事务中从 schema v1 添加展示时间、用户原话、原话文件位置和确认时间，升级为 schema v2；原任务、回报、摘要、序号、派发接收记录和历史 done 项均保留。未完成的 pending/handling 项不会自动变成 done，旧 done 项也不会被伪造用户确认。

已升级的 schema v2 不支持直接用 v0.1.0 打开；回退应在暂停后恢复升级前备份。当前等待用户的状态在暂停、重启和重新打开数据库后保持。显式更换总控绑定仍需重新激活 skill，但不会自动再次展示等待用户的结果。

## 开始使用

### 1. 指定总控

启动、查看状态和暂停等命令应在运行中的目标 Herdr 会话内执行。先用 `herdr agent list` 找到想作为总控的代理名或窗格 ID；目标必须已运行，且有原生 `agent_session`。

选择该代理窗格，通过 Herdr 插件动作 **Orchestrator: use this agent as coordinator** 指定总控。也可以先明确聚焦该代理，再执行：

```sh
herdr agent focus <代理名或窗格ID>
herdr plugin action invoke start --plugin herdr-orchestrator
```

start 绑定的是 **Herdr 当前聚焦的代理**，不是任意运行命令的 shell。总控正在工作时，启动消息会等它空闲再投递。总控读内置 skill 并执行消息中的 `ready` 后，才完成激活；动作命令返回成功只代表已启动动作。

### 2. 直接向总控发任务

普通使用只需要向总控发送自然语言任务，无需手工调用底层派发或回报命令。例如：

```text
请把下面两项任务分别交给 worker：
1. 在 /tmp/orch-demo/hello.txt 写入 hello，并回报实际文件内容。
2. 在 /tmp/orch-demo/world.txt 写入 world，并回报实际文件内容。
只允许创建和读取 /tmp/orch-demo 下的文件；不要访问业务仓库或外部服务。
优先复用合适的空闲代理，没有时启动 worker。
完成后一次只展示一条，等我处理完并确认，再主动通知下一条。
```

总控选择空间和 worker，发送目标、范围、授权与验收条件。插件附加接收确认和回报命令；worker 明确确认接收后执行任务，完成或阻塞时写结果并回报。后台轮询在总控空闲时唤醒它，按 FIFO 处理回报。第一条展示后持久进入 awaiting_user；即使第二条已回报，也保持 pending，不会一起展示。

处理完当前条时，直接说「这条处理完了」或「继续下一条」。总控记录当前 event ID 和你这条消息的原文，确认成功后结束本回合；后台轮询再主动唤醒总控展示下一条，不连续清空队列。普通新任务、无关消息、仅仅追问或补充信息都不自动视为完成确认。

对同一任务继续补充时直接说清关联，例如「把刚才 hello.txt 的内容改为 hello v2，继续交给原 worker，授权范围不变」。总控通常保留 task ID 并复用原 worker；原 worker 忙时等待其空闲。追问本身不关闭当前条，仍需明确允许继续下一条。也可以一次说清「这条已处理完，请按上述补充继续交原 worker，并继续下一条」：总控先登记关联当前条的后续派发，再保存确认；后续 worker 全部 accept 后才出队，不等执行完成。用户指定其他代理或需要多个专用 worker 时可以按实际分工派发。

用户确认先于后续 worker 接收时，原文和确认时点先保存，接收等待跨重入和重启保留，不要求用户再回复一次。接收后来到达时自动出队，下一条仍由轮询通知。等待用户时不定时催问；已确认但后续接收出现 blocked/missing/replaced 时，只提醒检查当前接收问题，不重复展示已确认结果、不提前展示第二条。

所有需要这种逐条展示的任务都要通过插件派发/回报协议。直接使用原生 `herdr agent prompt --wait` 读取的终端回复不会入队，不能代替插件 FIFO；多次问候等简单任务也遵循同一边界。

### 3. 查看进度、暂停和恢复

```sh
# 打开状态窗口
herdr plugin action invoke status --plugin herdr-orchestrator
# 暂停当前 Herdr 会话的编排；保留代理、回报和任务历史
herdr plugin action invoke stop --plugin herdr-orchestrator
```

状态窗口展示总控、后台 PID、最近轮询、待处理回报、派发接收情况和错误。`待接收` 与 `已接收` 是明确协议状态；`已回报完成` 才代表 worker 已回报业务结果。关闭窗口只关闭视图。恢复时重新聚焦原总控，执行 start；其原生会话身份必须仍然相同。

```sh
# 检查 start/stop/status 动作的实际退出码、stdout 和 stderr
herdr plugin log list --plugin herdr-orchestrator
```

一个 Herdr 会话只能绑定一个总控；不同命名 Herdr 会话各自有独立绑定和 FIFO。临时暂停本会话使用 stop；`herdr plugin disable herdr-orchestrator` 会禁用插件，影响使用该插件的会话。重新启用后在相应会话对原总控执行 start。

## 故障排查

| 现象 | 检查和处理 |
| --- | --- |
| Python 或 Herdr 版本不满足要求 | 检查上面的版本命令，先安装满足要求的运行环境。 |
| `agent has no native session identity` | `herdr agent get <目标>` 检查 `agent_session`；检查 integration，并在新启动的代理中发送第一条消息后重试。 |
| start 指定了错误的总控 | 先 stop，在启动消息提供的完整入口中显式 `bind --target <新目标> --rebind`；旧任务会保留。 |
| 已启用但没有启动消息 | 检查动作日志和状态窗口；总控 working/blocked/unknown 时不会唤醒，不代答未授权审批。 |
| worker 长时间 `待接收` 或投递超时 | 检查派发 ID、`herdr agent get/read <目标>`；超时不等于没有执行，不盲目重新派发。 |
| 从本地 link 切换到 GitHub 安装失败 | 暂停已有编排，`herdr plugin unlink herdr-orchestrator` 后重新安装；源代码保留。 |

需要卸载时，先暂停编排，再执行 `herdr plugin uninstall herdr-orchestrator`。保留需要的任务历史与结果文件备份；插件不会主动删除自己的数据库，也不会关闭 worker。

## 处理规则

- 每次派发携带目标、范围、授权和验收条件，生成独立 task ID 与 dispatch ID。
- Worker 先明确 `accept`，再执行任务，以 `completed` 或 `blocked` 回报。终端提交成功、代理空闲均不算业务完成。
- 回报正文、消息 ID 和 FIFO 序号在同一 SQLite 事务中保存。相同消息 ID 和内容重试不会重复入队；不同内容复用 ID 会报错。
- 一个 Herdr 会话只有一个当前队首，状态为 pending → handling → awaiting_user → done。next 重入保留状态，present 持久保存展示摘要，ack 保存当前条的实际用户原话及确认时间。
- 出队需要用户明确确认，以及该条所有未撤销后续派发均已 accept，**不等待执行完成**。确认先保存，未接收时队首仍保留；最后一份接收记录到达后自动出队。确认之后不能再向该条追加 parent-event 派发。
- 每 5 秒轮询原生状态，仅在总控 idle/done 时尝试唤醒；同一待处理通知最多每分钟重试一次。已展示且未确认的队首始终静默，不通知第二条。已确认的接收异常只提醒检查当前条；工具审批不代答。
- 同一原生 worker 会话同时只允许一个未结束的派发。新任务优先复用合适的空闲 worker；同任务补充保持原 task ID 和 worker。
- 暂停后仍可接收已派发 worker 的回报。重新 start 恢复；Herdr 启动钩子恢复已启用的绑定。代理身份变化需暂停后显式重新绑定。

以下是总控和 worker 调用的底层协议，普通用户无需手工执行。总控收到的启动消息包含完整的插件入口命令（下文记作 `CLI`），带有绝对程序路径、状态目录和会话 socket。`CLI` 是文档占位词，并非安装后新增的系统命令；执行时替换为启动消息中的完整命令。其主要命令为：

```text
CLI ready
CLI dispatch --target WORKER --task-file TASK.json
CLI wait-accepted --dispatch DISPATCH_ID --timeout 10
CLI next
CLI present --event SEQ --summary-file /ABS/SUMMARY.txt
CLI dispatch --target WORKER --task-file TASK.json --task-id TASK_ID --parent-event SEQ
CLI ack --event SEQ --reply-file /ABS/USER_REPLY.txt
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

Worker 命令由派发消息提供。结果、摘要和用户原话文件必须是绝对路径的 UTF-8 文件。结果和用户原话保存路径与正文，摘要保存正文；源文件删除后正文仍保留。总控可直接读取 next 返回的数据。present 对相同摘要重试幂等，不允许替换已登记展示的摘要；ack 对同一 event 和相同原文重试幂等，不改变原确认时间。错误 event、尚未展示的队首、空白原文、冲突确认或其他会话调用均拒绝。已 done 事件的相同 ack 重试只返回旧记录，不会确认新的队首。旧 finish 命令已删除，不能用自动总结出队。

确认原文必须取自最新实际用户消息，不能使用模型总结、超时、代理空闲、worker accept 或另一条任务的答复代替。CLI 是总控记录消息的入口：程序验证调用代理会话、当前 event 和状态，但无法证明原文确实来自真人，也无法自行理解自然语言确认意图；这部分由总控遵循内置 skill。检查后撤销派发可用 `CLI abandon --dispatch ID --reason 原因`；撤销不会终止执行代理，应先核实其实际状态和后续处置。

## 数据与边界

状态保存在 Herdr 提供的 `HERDR_PLUGIN_STATE_DIR`，包括 `state.sqlite3`、WAL 文件以及每个会话的锁与日志。任务授权、结果、摘要和确认原文均留在本机，不上传到 GitHub。卸载前需要的数据应单独备份；插件不自动清理历史记录。

队列顺序指 SQLite 成功写入的先后顺序。数据库事务保证单一队首及持久用户确认、后续接收门槛；总控职责、领域路由、用户消息真实性与业务正确性仍由 skill 与代理执行。插件不是代理权限沙箱。

present 登记和聊天消息展示也不能共享事务。总控须在本回合完成持久登记与一次展示；极端中断时需核对摘要及聊天记录。已登记 awaiting_user 的队首不会被轮询自动再次展示。

Herdr 目前没有原子的「仍然空闲才提交」条件接口；读取状态与终端提交之间存在竞争。终端投递和数据库也不能共享事务，因此不承诺业务动作恰好执行一次。投递失败或超时会保留派发 ID 并明确报错，先查记录和 worker，再决定是否撤销或继续，程序不会自动重派业务任务。

已启用绑定依赖同一原生代理会话身份。如果总控被替换，程序拒绝把旧任务自动交给新代理；暂停后用完整入口命令显式 `bind --target ... --rebind`。移动未命名代理到其他空间后，窗格 ID 会变化，需检查绑定或重新绑定。总控忙时唤醒会推迟；宿主退出期间不会投递消息。

## 开发与验证

```sh
python3 orchestrator.py check-runtime
python3 -m unittest discover -s tests -v
herdr plugin link "$PWD"
```

同一插件 ID 的 GitHub 安装版与本地 link 不能混用。切换开发版本前，先暂停相关会话的编排并卸载安装版，再 link；插件注册表由本机共享，命名 Herdr 会话只隔离总控绑定和 FIFO，不隔离插件安装状态。

v0.2.0 的本地 unittest 覆盖真实 SQLite 状态、进程并发和 CLI 子进程，传输使用 FakeHerdr/模拟 CLI：

- 两条同时到达只通知第一条；等待用户超过 60 秒不重复催问，第二条保持 pending；实际确认后由轮询通知第二条。
- 拒绝错误 event、未展示确认、冲突确认和旧 finish；同原文重试不改变确认时点或另一条队首。
- 确认先于接收且跨暂停/重启不丢失；后续 accept 前保留队首，accept 后不等待 completed 即出队；blocked/missing/replaced 只唤醒已确认条的接收检查。
- 显式 rebind 时仍发送新总控的激活提示；等待用户不阻断激活，也不重复展示结果。
- schema v1 无损迁移、历史摘要与未完成状态保留；会话身份隔离、多个专用 worker、幂等回报、代理复用及不明投递仍有覆盖。

2026-10-10，在 Linux、Herdr 0.9.3、Codex 0.162.1 的独立 Herdr 会话中，使用 v0.2.0 运行时、一个真实 Codex 总控和两个真实 Codex worker 验证了上述通知流程：两个独立问候均通过 dispatch/accept/report 完成，总控先只展示 A，B 保持 pending；A 等待用户超过两分钟时仍没有重复提醒或展示 B。输入模拟用户原话「这条处理完了，继续下一条。」后，A 出队，后台轮询另起一轮通知 B；B 展示后继续等待用户，没有自动确认。宿主回读核对了数据库时序、确认原文及原结果文件与持久正文的一致性。

上述模型和终端投递是真实的，用户处理完成消息是隔离测试输入，未冒充正式会话的用户确认。尚未实测 macOS、真实审批 UI、整个 Herdr 服务重启后的模型恢复，以及正式用户亲自完成两条回报的交互。此前 v0.1.0 的串行汇总验证不包含用户处理完成门槛，不能替代本版验证。

协议与接口依据：[Herdr Socket API](https://herdr.dev/docs/socket-api/)。

MIT License。
