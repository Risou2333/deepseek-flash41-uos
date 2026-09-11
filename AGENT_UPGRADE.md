# USO 2.0 工程 Agent：实施、验收与部署

本次以 `Risou2333/deepseek-flash41-uos` 的 `18ee9bb42da0f40f3a03ef8b3932f876a06570a2` 为基线，对照上传的 10 份 USO Agent 文档实施。版本从 1.1.0 升级为 2.0.0。保留原有办公聊天、图片与文档提取、首次网页配置、密码登录、历史数据库和 API 诊断。

## 1. 已实现与规划修正

| 文档要求 | 本次实现 | 调整理由与边界 |
|---|---|---|
| 工程目录、文件树、索引 | 本机路径打开、最近工程、分页文件列表、刷新索引 | 不上传完整工程；Python 服务必须运行在工程所在电脑 |
| 文件读写与补丁 | 分页 UTF-8 读取、sha256、创建文件、唯一文字替换、unified diff | 不支持直接覆盖未知版本；提供预览、接受、拒绝 |
| 备份与撤销 | SQLite 保存原文和新文；原子替换；逐项撤销；崩溃状态恢复 | 新建文件撤销时删除；有后续编辑则阻止撤销，不强制覆盖 |
| DeepSeek Tool Calling | SSE 工具参数分片合并、多工具顺序执行、结果回传、完整思考字段回传 | 使用正式 `/chat/completions`，不添加 beta strict 限定 |
| Agent 循环 | 查询、修改申请、审阅、执行命令、继续推理、总结 | 默认最多 24 个模型请求，可选 1–80；每轮输出 16384 tokens |
| Planner / Reviewer | 单控制器提示词、工具轨迹、实际命令退出码与验证记录 | 避免仅为模块名称拆出重复的多模型调用；“模型结束”不代表测试一定通过 |
| Terminal | argv 数组、命令白名单、逐条批准、精确绑定审批编号、输出和时间限制 | 不开放任意 shell 字符串；允许的解释器仍可运行任意代码，不能冒充 OS 沙箱 |
| Git | status、相对 HEAD 的跟踪文件 diff、持久化诊断 checkpoint | 不自动 `git add`、commit、reset、checkout、push；避免触碰原有暂存状态和未完成工作 |
| Git rollback | 使用每次已接受修改的独立文件备份 | checkpoint 不含未跟踪文件正文，也可能截断，不能用作完整仓库备份 |
| Memory | 工程独立的可编辑长期记忆、模型记忆工具、最近三次任务摘要 | 记忆与任务记录是可核对资料；不把模型摘要当作已经验证的事实 |
| RAG / Context | 文件元数据索引、轻量文字检索、按需读取、完整工具组归档与上下文重建 | 暂不引入 FAISS、Chroma、embedding 服务；不能无限扩大模型上下文 |
| workspace JSON 文件 | 独立 SQLite records 表存储项目、索引、记忆、任务、备份和协议归档 | 便于事务与恢复；不往用户工程写入客户端索引、密钥或历史 |
| API Key 加密 | 沿用 700 数据目录、600 配置与数据库、PBKDF2 密码哈希 | 未实现磁盘加密；不把编码、混淆或与密钥同机存放的固定密钥包装成加密保护 |
| UOS / MIPS64 | Python 3.7+ 标准库、HTML/CSS/JS，无 Flask/FastAPI/Electron 运行时依赖 | 实机架构和 Python 3.7.3 仍需目标机验收 |
| DEB 部署 | 修复 `protocol.py` 漏装；收录全部新增模块与静态文件；统一 2.0.0 版本 | 支持直接从文件夹运行，DEB 是可选路径 |

## 2. 启动与使用

1. 停止旧服务。另存整个原数据目录，保留旧源码目录。
2. 取得本升级分支的完整源码；不要只替换 `app.py`。
3. 在项目源码目录运行 `bash start.sh`。继续使用已有本地密码和 API Key；新安装仍由网页初次配置。
4. 从聊天页点击“打开工程 Agent”，或访问 `http://127.0.0.1:8765/agent`。
5. 输入具体工程目录，例如 `/home/casic/projects/demo`。不要选择整个用户目录、系统目录或含客户端数据的上级目录。
6. 输入任务并开始。模型可以主动检索、读取工程；修改提案在接受前不会落盘。
7. 审阅 diff 后接受或拒绝。测试命令另行展示完整 argv、目录、用途和超时，确认后执行一次。
8. 查看任务事件、命令输出、退出码、返回模型与 usage；必要时继续提出修复任务。停止/结束任务后可以逐项撤销文件修改。

审批后模型会继续产生 API 请求，按平台实际用量计费。缺失 usage 保留为 `null`，不能解读为零消耗。每次模型请求记录服务端返回的模型、response ID、finish reason、usage 和耗时。不后台发送计费探针、不自动重试失败请求。

示例任务：

> 先读取 README 和测试，定位登录校验逻辑。修复确认存在的问题，提出最小补丁，执行相关测试。列出修改文件、命令退出码以及未验证的部分。

可运行的命令：`python3`、`python`、`pytest`、`make`、`cmake`、`node`、`npm`，实际是否安装以本机为准。以数组调用，例如 `["python3", "-m", "unittest", "discover", "-s", "tests", "-v"]`。没有默认安装这些额外程序；Python 后端运行不需要 Node/npm。

## 3. 代码与接口

| 文件 | 职责 |
|---|---|
| `app.py` | 原认证、聊天及配置；增加工程静态资源和已登录 API 路由 |
| `project_manager.py` | 工程路径校验、元数据索引、分页读取、文字检索、SQLite 存储 |
| `tools/filesystem.py` | 写入提案、diff、sha256 冲突检查、原子写入、撤销、恢复 |
| `tools/terminal.py` | 受批准的 argv 进程、环境变量清理、超时、取消、输出限额 |
| `tools/git.py` | 只读 Git 检查及诊断快照；默认敏感路径过滤 |
| `agent/controller.py` | 工具 schema、协议、循环、审批、任务状态、上下文归档 |
| `agent.html`、`static/agent.js`、`static/agent.css` | 独立工程工作台；与办公聊天共享本地登录 |
| `tests/test_agent.py` | 工具、审批、恢复、Git、协议和 HTTP 集成测试 |

工程 API 均为已登录 POST JSON，同时要求正确 Host、同源 Origin 与 `X-Requested-With: DeepSeekClient`。`project` 是打开工程返回的 ID，不允许模型任意切换工程根目录。

| API | 主要请求字段 | 行为 |
|---|---|---|
| `/api/project/open` | `path` | 注册/恢复工程并扫描 |
| `/api/project/list` | 无 | 最近工程 |
| `/api/project/tree` | `project`, `offset`, `refresh` | 文件列表，每页 500 条 |
| `/api/project/search` | `project`, `query`, `glob` | 文字检索 |
| `/api/file/read` | `project`, `path`, `start`, `lines` | 正文页、完整文件 sha256、下一页 |
| `/api/file/write` | `project`, `path`, `content`, `expected_sha` | 返回待审阅提案，不直接写入 |
| `/api/file/patch` | `project`, `path`, `old`, `new`, `expected_sha` | 唯一匹配替换提案 |
| `/api/file/decide` | `change`, `accept` | 处理手动提案；活跃任务期间禁用，须用 Agent 审批 |
| `/api/file/rollback` | `change` | 校验当前内容后撤销已接受修改 |
| `/api/project/changes` | `project` | 提案、diff、状态 |
| `/api/project/memory` | `project`, 可选 `content` | 读取/更新记忆；任务期间手动更新禁用 |
| `/api/project/git` | `project`, `action: status/diff` | Git 检查 |
| `/api/agent/start` | `project`, `message`, `effort`, `max_steps`, `context_chars` | 创建后台任务 |
| `/api/agent/get` | `run` | 获取进度、待审批动作、事件和用量 |
| `/api/agent/list` | `project` | 最近任务记录 |
| `/api/agent/approve` | `run`, `approval`, `accept` | 消费一次性审批，继续原任务 |
| `/api/agent/stop` | `run` | 取消；不撤销已经完成的操作 |

直接文件 API 服务于扩展集成；当前页面由 Agent 生成提案，提供审阅与撤销，没有通用手工代码编辑器。

模型工具只有 `list_directory`、`read_file`、`search_code`、`write_file`、`patch_file`、`execute_command`、`git_status`、`git_diff`、`git_checkpoint`、`memory_read`、`memory_write`。不存在模型可直接调用的“批准”工具。

## 4. 状态与恢复

任务状态：`analyzing` → `executing` → `waiting_approval` → 继续执行；终态为 `finished`、`failed`、`cancelled`、`interrupted`、`limit`。

- 只有完整 SSE `[DONE]` 且 finish reason 合法后才执行本轮工具。参数分片必须完整组装，多个工具按顺序执行并逐一匹配 `tool_call_id`。
- 思考模式的工具请求回传所有仍保留在当前协议上下文中的 `reasoning_content`。上下文过预算时，在全部工具结果完成后归档完整组，不截断半组协议。
- 上下文重建保留用户任务、工程记忆、最新任务摘要及最近工具结果节选，明确提示重新核对事实；这是有损压缩，不保证无限长会话无信息损失。
- 刷新页面不停止后台任务。重新选择工程可恢复最近任务和审批面板。
- 服务重启时未完成任务标记为 `interrupted`，未应用提案失效。不会重放请求、命令或已消费审批。
- 写入前保存 `applying` 状态及完整原文、新文。重启后仅对比文件恢复记录状态，不重做写入；不匹配则标记 `conflict`，原文备份仍在数据库。
- 撤销也先保存 `reverting` 状态。已经发生外部后续编辑的文件不强行撤销。
- 等待审批时停止会拒绝当前待写提案。命令运行时停止会杀死当前进程组；无法撤销命令已经产生的副作用，也无法保证捕获主动脱离进程组的子进程。
- 网络读取的取消可能等待至 90 秒；已被平台接收的请求仍可能计费。keepalive 行也会检查取消与轮次时限。

## 5. 预算与安全边界

| 项目 | 当前边界 |
|---|---|
| 同时活跃 Agent | 1 个；普通办公聊天仍沿用自身串行锁 |
| 索引 | 最多 20000 文件 / 10 秒；明确返回 truncated |
| 文件 | 普通、非硬链接、非符号链接、UTF-8 文本，最大 2 MiB |
| 读取 | 默认 200 行，最多 500 行，每页最多 24000 字符；长行可能截断 |
| 搜索 | 大小写不敏感文字匹配，最多 60 处 / 5 秒，非正则/向量搜索 |
| 上下文 | 48000 / 96000 / 192000 字符本地预算，不等同官方 token 上限 |
| 模型轮次 | 默认 24，最多 80；单轮最多 16 个工具调用 |
| 模型输出 | 每轮请求 16384 tokens，25 万字符本地响应保护，15 分钟轮次时限 |
| 命令 | 默认 120 秒，范围 1–300 秒；输出合计 64 KiB，达限停止 |
| Git diff | 只展示默认策略允许的跟踪文件，最多 200 个；输出受 64 KiB 限额 |
| 记忆 | 每工程 12000 字符 |
| UI 历史 | 最近 100 个工程、全局最近 1000 条任务/修改中的本工程记录；更早数据不自动删除 |

默认排除 `.git`、依赖/缓存目录、隐藏目录扫描、`.env*`、私钥后缀、`credentials*`、`config.json`、数据库等。此处采用内置排除策略，**尚未解释 `.gitignore` 或提供自定义忽略编辑器**。敏感文件过滤是有限规则，不能识别任意普通代码中硬编码的密码；将工作资料交给云模型前仍需由使用者判断工程内容。

路径检查、批准机制、命令白名单与超时不是内核隔离。已批准的 Python/Node/构建命令可以执行项目代码，拥有本机用户权限，并可能访问网络。执行环境不继承 API Key、PYTHONPATH、NODE_OPTIONS 等变量，但这不阻止被批准程序主动读取当前用户可读文件。应用只适用于可信本机单用户；不应公开服务，也不应把不可信工程当作沙箱运行。路径检查不承诺抵御另一个恶意本机进程并发替换目录的竞态。

不新增防火墙、容器或 root 权限依赖，不修改系统 Python，不绕过 TLS 验证。更换 Key/密码前须结束 Agent 任务。原聊天记录与新任务协议分别存放。

## 6. 数据与回退

默认路径：

```text
~/.local/share/deepseek-client/
  config.json
  history.db
  history-before-v1.1.db  # 若此前已迁移
  agent/
    agent.db
```

`agent.db` 中的记录类型为 `project`、`index`、`run`、`change`、`transcript`、`checkpoint`。项目记忆存于 project，索引存元数据，内容 sha256 在读取/修改时计算；不会为所有扫描文件逐个读全量正文。历史与备份目前没有自动清理策略；大工程长时间使用时数据库会增长。

回退应用：停止服务，另存当前整个数据目录，换回旧版完整源码，再启动。旧聊天库格式沿用 1.1；旧版忽略新 `agent` 目录。**应用版本回退不会恢复已被 Agent 修改的工程文件**；应先从修改记录逐项撤销，或使用用户自己的 Git/备份流程。命令副作用不能自动回退。

## 7. 验证与目标机验收

自动验证：

```bash
python3 -m unittest discover -s tests -v
node --check static/agent.js   # 仅开发验收时需要 Node，运行应用不需要
bash -n start.sh build-deb.sh diagnose.sh
bash build-deb.sh             # 可选，需要 dpkg-deb
```

自动测试不调用真实付费 DeepSeek API。覆盖碎片化工具参数、多工具结果匹配、思考回传、usage、读取→补丁审阅→命令审阅→实际本地断言测试→总结完整闭环、拒绝/重复审批、断流、停止、轮数限制、上下文归档、路径越界/敏感文件/链接、补丁与撤销冲突、写入后重启恢复、Git 敏感内容过滤、HTTP 登录与同源防护，以及原客户端回归。

尚未完成的验证：真实 DeepSeek Key 的工具调用、UOS 20 MIPS64 / Python 3.7.3 实机运行、真实浏览器交互与视觉验收。当前环境浏览器内核下载超时，因此不能宣称前端端到端验收通过。

建议实机在一个可丢弃的测试工程里验收：

1. 新建 `demo.py`：`value = 1`；新建测试断言 value 应为 2。
2. 打开该目录，要求 Agent 找错、给补丁、运行测试。
3. 在接受前核对磁盘仍是 1；拒绝一次，核对没有写入。
4. 新任务提出正确补丁，接受后核对磁盘为 2；批准 `["python3", "-m", "unittest", "discover"]`。
5. 检查退出码 0、实际输出和每次调用的 usage；结束后撤销，核对磁盘恢复为 1。
6. 创建另一个待审批任务，重启服务，核对任务标记中断且未重复执行。

## 8. 官方依据

核对日期：2026-09-11。Agent 沿用 `deepseek-flash`，不根据回复口吻推断版本。

- [模型映射与计费](https://api-docs.deepseek.com/quick_start/pricing/)
- [工具调用协议](https://api-docs.deepseek.com/guides/tool_calls/)
- [思考模式与工具对话 reasoning_content 回传要求](https://api-docs.deepseek.com/guides/thinking_mode/)

后续扩展应按实际需要增加：可选 OS 级隔离、可配置忽略策略、历史清理、更多文件格式、向量检索、Git 提交审阅流程。上述项目未伪装成本次已实现能力。
