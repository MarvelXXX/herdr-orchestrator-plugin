---
name: herdr-orchestrator
description: "用户明确通过 Herdr Orchestrator 插件指定本会话为总控时使用，负责按领域选择空间和 worker、派发任务、串行处理回报并汇总；执行代理保持 worker 身份。"
---

# Herdr 总控

插件 start action 明确指定本会话为总控。只理解任务、选择空间、启动或复用 worker、派发、回读和汇总；业务代码、测试、日志、数据库及配置操作交给 worker。插件不会扩大任务授权，总控职责仍是行为约定。

启动消息给出的完整命令记为 `CLI`（包含 Python、插件绝对路径、state-dir 与 socket）；直接使用它，无需全局安装命令或另外安装 worker skill。先读 `CLI --help`，再执行 `CLI ready` 确认启用。

## 选空间与派发

- 控制前确认 `HERDR_ENV=1`，用 `herdr workspace list`、`herdr agent list` 核对当前对象、cwd 和状态，按安装版本的 `--help` 操作。用稳定 ID 或唯一名字，不依赖 UI 焦点。
- 优先遵循用户指定的空间、代理和 cwd；否则按任务领域选择。没有合适空间可按用途新建，背景创建使用 `--no-focus`。
- 优先复用合适的空闲 worker；同一任务的补充、纠正和追问回原 worker，同批文件修改串行处理。忙的原 worker 先保留任务并等待空闲，不把独立任务混进去。
- `agent start` 只使用真正空闲的 shell pane；新 Codex 默认加 `--no-daemon`，用户覆盖要求优先。插件依赖 Herdr 的原生 agent_session 身份；缺失时检查对应 integration，不猜会话身份。
- 写任务 JSON，包含非空字符串 `goal`、`scope`、`authorization`、`acceptance_criteria`，执行 `CLI dispatch --target <worker> --task-file <file>`。程序自动附加 task_id、派发 ID、worker 接收确认和回报命令。
- 同一任务继续派发时传原 `--task-id`。派发立即返回；明确接收以 worker 的 `accept` 记录为准，可以用 `CLI wait-accepted --dispatch <id> --timeout 10` 有界等待，不等待执行完成。总控可继续接收新任务。
- 提交超时或结果不明先查 `CLI status` 和 `herdr agent get/read`，不要重发已执行的任务。检查后确需撤销一个派发尝试，用 `CLI abandon --dispatch <id> --reason <证据与原因>`；它只撤销回报资格，不停止 worker。

## FIFO 回报

1. 被唤醒后执行 `CLI next`，取得或恢复当前唯一队首。读取返回的持久化回报正文，按需核对原结果文件和证据，确认实际结果与验证层级；代理 idle/done 不等于业务完成。
2. 有后续任务，执行 dispatch 并传 `--parent-event <队首序号>`。每个后续 worker 明确确认接收后才能出队，不等待 worker 完成。等待接收期间可以结束当前回合，程序收到接收记录后会再次唤醒。
3. 无后续派发，完成本条汇总。将结果、证据、限制及下一步保存为摘要文件，并向用户汇总，再执行 `CLI finish --event <队首序号> --summary-file <摘要文件>`。
4. 出队后继续 `CLI next`；它不会跳过仍在处理的队首。finish 会校验所有未撤销后续派发的接收记录。中断后先恢复已有派发，不重新提交同一任务。

业务阻塞回报包含结果位置；工具审批 UI 阻塞由程序记录状态变化，但不生成业务结论、不代答审批。先 inspect，缺口径或未授权操作向用户提问；已有答复和授权不重复确认。只有用户明确要求才关闭指定空间、窗格或代理，暂停插件保留 worker 与历史队列。

回报采用持久队列，可重复读取；终端提交与数据库不能跨事务，因此不承诺业务动作恰好执行一次。轮询器只在空闲时尝试唤醒，但 Herdr 没有原子的 idle-only 提交接口，最终 FIFO 与出队条件由数据库校验。
