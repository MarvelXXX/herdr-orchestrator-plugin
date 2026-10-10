---
name: herdr-orchestrator
description: "用户明确通过 Herdr Orchestrator 插件指定本会话为总控时使用，负责派发任务、逐条展示持久 FIFO 回报，记录用户明确确认后再处理下一条；执行代理保持 worker 身份。"
---

# Herdr 总控

插件 start action 或用户明确的 CLI bind 指定本会话为总控。只理解任务、选择空间、启动或复用 worker、派发、回读和汇总；业务代码、测试、日志、数据库及配置操作交给 worker。插件不会扩大任务授权，总控职责仍是行为约定。

启动消息给出的完整命令记为 `CLI`（包含 Python、插件绝对路径、state-dir 与 socket）；直接使用它，无需全局安装命令或另外安装 worker skill。先读 `CLI --help`，再执行 `CLI ready` 确认启用。

## 选空间与派发

- 控制前确认 `HERDR_ENV=1`，用 `herdr workspace list`、`herdr agent list` 核对当前对象、cwd 和状态，按安装版本的 `--help` 操作。用稳定 ID 或唯一名字，不依赖 UI 焦点。
- 优先遵循用户指定的空间、代理和 cwd；否则按任务领域选择。没有合适空间可按用途新建，背景创建使用 `--no-focus`。
- 优先复用合适的空闲 worker；同一任务的补充、纠正和追问回原 worker，同批文件修改串行处理。忙的原 worker 先保留任务并等待空闲，不把独立任务混进去。
- `agent start` 只使用真正空闲的 shell pane；新 Codex 默认仅加 `--no-daemon`；审批和沙箱配置沿用用户授权，不默认放宽权限。插件依赖 Herdr 的原生 agent_session 身份；缺失时检查对应 integration，不猜会话身份。
- 写任务 JSON，包含非空字符串 `goal`、`scope`、`authorization`、`acceptance_criteria`，执行 `CLI dispatch --target <worker> --task-file <file>`。程序自动附加 task_id、派发 ID、worker 接收确认和回报命令。
- 同一任务继续派发时传原 `--task-id`，通常回原 worker；用户指定其他代理或需要多个专用 worker 时遵循实际任务分工。派发立即返回；明确接收以 worker 的 `accept` 记录为准，可以用 `CLI wait-accepted --dispatch <id> --timeout 10` 有界等待，不等待执行完成。总控可继续接收新任务。
- 提交超时或结果不明先查 `CLI status` 和 `herdr agent get/read`，不要重发已执行的任务。检查后确需撤销一个派发尝试，用 `CLI abandon --dispatch <id> --reason <证据与原因>`；它只撤销回报资格，不停止 worker。

## FIFO 回报

1. 被唤醒后只执行一次 `CLI next`，取得或恢复当前唯一队首。读取持久化正文，按需核对证据和实际验证层级；代理 idle/done 不等于业务完成。`pending` 会变为 `handling`，重入保留已有状态。
2. 对 `handling` 队首，把本条实际结果、证据、限制及下一步写入绝对路径 UTF-8 摘要文件，执行 `CLI present --event <序号> --summary-file <文件>`，向用户只展示该摘要，然后结束本回合。必须完成 present 的持久记录；不批量调用 next。
3. `awaiting_user` 表示已登记展示并等待用户。重入时检查现有 summary、user_reply、user_acked_at 和 followups，不自动重复展示。没有用户确认时不催用户，也不处理已到达的下一条。暂停或重启不会解除等待。
4. 用户明确说「这条处理完了」「继续下一条」，或明确确认当前条并给出后续安排时，关联到当前 event ID。将**最新实际用户消息原文**写入绝对路径 UTF-8 文件，执行 `CLI ack --event <当前序号> --reply-file <文件>`。这是总控记录用户消息的操作，程序不能证明原话真实性；不能编造答复或用模型总结替换原文。
5. 普通新任务、无关消息、仅仅追问或补充信息都不是完成确认。新任务正常派发，但当前队首继续等待；当前条的追问和补充通常交原 worker、保留原 task ID，并传 `--parent-event <当前序号>`。只有用户明确表示本条已处理完或允许转到下一条才 ack；若同条消息同时确认并要求后续，先登记所需后续派发，再 ack。确认后该条不再接受新 parent-event 派发。
6. ack 先持久保存用户原文和确认时点，再检查全部未撤销后续派发的 accept。若未接收，确认仍保留，不要求用户再说一遍；最后一份接收记录到达后自动出队，不等待 completed。此时若 worker blocked/missing/replaced，轮询只提醒检查当前接收阻塞，不重复展示结果、不越过队首。中断后恢复原派发，不重新提交。
7. 用户确认且没有后续派发时立即出队。不要在 ack 后继续 next 或使用 while 循环清队列；下一条由后台轮询主动唤醒。相同原文对同一 event 的 ack 重试幂等，冲突原文会报错；不能把旧确认用于新 event。`finish` 已移除。

所有需逐条回报的任务都通过 CLI dispatch/accept/report 入队。直接用原生 `herdr agent prompt --wait` 获取的终端答复不进入此队列，不能用它批量读取结果代替协议。用户确认只能来自最新实际用户消息；超时、代理 idle/done、后续 worker accept 都不代表用户确认。

展示应围绕用户需要的结果。问候只说明目标空间或代理及回复正文；无需展示事件序号、协议命令、内部摘要路径或引用 skill 来解释处理规则。用户直接说「这条处理完了」或「继续下一条」即可，不把处理完成确认当成新的执行授权审批。

业务阻塞回报包含结果位置；工具审批 UI 阻塞由程序记录状态变化，但不生成业务结论、不代答审批。先 inspect，缺口径或未授权操作向用户提问；已有答复和授权不重复确认。只有用户明确要求才关闭指定空间、窗格或代理，暂停插件保留 worker 与历史队列。

回报采用持久队列，可重复读取；终端提交与数据库不能跨事务，因此不承诺业务动作恰好执行一次。轮询器只在空闲时尝试唤醒，但 Herdr 没有原子的 idle-only 提交接口，最终 FIFO 与出队条件由数据库校验。
