<div align="center">

```
 █████╗ ███████╗████████╗██████╗ ███████╗ █████╗        █████╗  ██████╗ ███████╗███╗   ██╗████████╗
██╔══██╗██╔════╝╚══██╔══╝██╔══██╗██╔════╝██╔══██╗      ██╔══██╗██╔════╝ ██╔════╝████╗  ██║╚══██╔══╝
███████║███████╗   ██║   ██████╔╝█████╗  ███████║█████╗███████║██║  ███╗█████╗  ██╔██╗ ██║   ██║   
██╔══██║╚════██║   ██║   ██╔══██╗██╔══╝  ██╔══██║╚════╝██╔══██║██║   ██║██╔══╝  ██║╚██╗██║   ██║   
██║  ██║███████║   ██║   ██║  ██║███████╗██║  ██║      ██║  ██║╚██████╔╝███████╗██║ ╚████║   ██║   
╚═╝  ╚═╝╚══════╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝      ╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝   ╚═╝   
```

**面向自动化软件工程的自进化 Coding Agent**

![Python](https://img.shields.io/badge/Python-3.10--3.12-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-Apache_2.0-green)
![Version](https://img.shields.io/badge/Version-0.7.1-orange)

</div>

ASTrea 是一个具备分层记忆和自进化能力的终端 Coding Agent 框架。它以中心化的 Master + ReAct 循环为核心，通过分层上下文管理、双通道自进化和受控 Subagent 分支，解决 LLM 驱动的代码生成在长程任务中面临的上下文退化、经验遗失和调查噪声问题。


---

## 特性概览

- **分层记忆系统 (LayeredMemory)**  
  L1 冻结锚点 + L4 低频归档 + L2b 追加式对话链/状态机，三层物理拼接结构，支持零 LLM 调用的确定性上下文压缩，KV Cache 缓存命中率约 95%。

- **双通道自进化**  
  成功轨迹 → 模式挖掘 → 工具合成（程序性记忆）  
  间隔复现失败 → 约束生成 → 运行时注入（反思性约束）  
  配合熵减淘汰机制，防止工具库和约束库无限膨胀。

- **Subagent Fork**  
  Master 可 fork 完整上下文快照给只读调查分支，通过工具白名单和结构化 JSON 报告协议实现信息瓶颈，将调查噪声隔离在分支中。

- **多 Provider LLM 路由**  
  支持 Qwen / DeepSeek / GPT 等多模型热切换，兼容 OpenAI SDK 接口规范。支持深度思考模式、推理强度控制和 Token 费用追踪。

- **MCP 协议支持**  
  动态接入外部 MCP Server，运行时启用/停用/重启，工具自动注册到统一 Registry。

- **Skill 系统**  
  支持以 Markdown 定义的 Skill 片段注入 L1 层，按规则激活，不污染运行时上下文。

- **Rich TUI 终端界面**  
  基于 Rich + prompt_toolkit 的终端交互界面，支持 `/` 命令补全、上下文预算进度条、Token 消耗面板、风险操作确认选择器。

---

## 系统架构

```
┌──────────────────────────────────────────────────────────┐
│                      CLI (Rich TUI)                      │
│              prompt_toolkit 交互 · / 命令路由             │
└────────────────────────┬─────────────────────────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│                    Master (ReAct 循环)                    │
│         单一决策中心 · 工具调度 · 状态推进 · 自进化编排     │
├──────────┬──────────┬──────────┬──────────┬───────────────┤
│  Layered │   Tool   │  Self-   │ Subagent │    Skill      │
│  Memory  │ Registry │ Evolution│   Fork   │   Manager     │
│ L1+L4+L2b│ 18+ 工具 │ 轨迹挖掘 │ 只读调查 │ Markdown 注入 │
│ 确定性压缩│ +MCP+合成│ +约束生成│ 信息瓶颈 │ 按规则激活    │
└──────────┴──────────┴──────────┴──────────┴───────────────┘
                         │
┌────────────────────────▼─────────────────────────────────┐
│                  LLM Client (多 Provider)                 │
│      Qwen · DeepSeek · GPT · 自定义 Provider              │
│      思考模式 · Token 追踪 · KV Cache 优化                 │
└──────────────────────────────────────────────────────────┘
```

---

## 项目结构

```
ASTrea/
├── cli.py                  # TUI 入口（Rich + prompt_toolkit）
├── cli_commands.py         # / 命令路由与处理器
├── pyproject.toml          # 项目元数据与依赖
├── .env                    # Provider 密钥与模型配置
│
├── core/
│   ├── master.py           # Master ReAct 循环主控
│   ├── layered_memory.py   # 三层分层记忆 + 确定性压缩
│   ├── llm_client.py       # 多 Provider LLM 路由 + Token 追踪
│   ├── subagent.py         # Subagent Fork 只读调查
│   ├── evo_agent.py        # 自进化编排（工具合成 + 约束生成）
│   ├── pattern_miner.py    # 轨迹模式挖掘（三层过滤）
│   ├── tool_synthesizer.py # 工具合成 + 四级验证管线
│   ├── constraint_store.py # 反思性约束管理
│   ├── trajectory_logger.py# 轨迹与失败事件采集
│   ├── memory_db.py        # SQLite 持久层
│   ├── mcp_manager.py      # MCP Server 生命周期管理
│   ├── skill_manager.py    # Skill 扫描与激活
│   ├── skill_runner.py     # Skill 执行引擎
│   └── tools/              # 内置工具集
│       ├── __init__.py     # ToolRegistry 统一注册
│       ├── read_file/      # 文件读取（read_file_tool.py + __init__.py）
│       ├── edit_code/      # 代码编辑（edit_code_tool.py + __init__.py）
│       ├── create_code/    # 文件创建（create_code_tool.py + __init__.py）
│       ├── grep_search/    # 文本搜索（grep_search_tool.py + __init__.py）
│       ├── run_command/    # 命令执行（run_command_tool.py + __init__.py）
│       ├── save_plan/      # 执行计划管理（save_plan_tool.py + __init__.py）
│       ├── recall_step/    # 历史步骤回溯（recall_step_tool.py + __init__.py）
│       ├── task_done/      # 任务完成信号（task_done_tool.py + __init__.py）
│       ├── subagent/       # Subagent 工具与 Prompt
│       └── _risk_guard/    # 风险操作分级拦截
│
├── config/
│   └── mcp_servers.json       # MCP Server 配置
│
└── prompts/
    └── master_system.md       # Master System Prompt 模板
```

---

## 快速开始

### 环境要求

- Python 3.10-3.12
- 至少一个 LLM API Key（DeepSeek / Qwen / GPT 等兼容 OpenAI SDK 的服务）

### 安装

```bash
# 1. 克隆仓库
git clone https://github.com/dfjmsf/ASTrea-AGENT.git
cd ASTrea-AGENT

# 2. 创建虚拟环境
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

# 3. 安装依赖
pip install -r requirements.txt

# 4.（可选）注册全局命令 astrea
pip install -e .
```

### 配置

首次启动后可在 TUI 中运行：

```text
/config init
```

命令会按顺序要求输入模型提供商、API Key、Base URL、模型名称，并自动生成用户级全局 `.env`：

```text
Windows: %APPDATA%\ASTrea\.env
Linux/macOS: ~/.config/astrea/.env
```

加载优先级：

```text
当前目录 .env > 用户级 .env > 系统环境变量
```

也可以手动创建项目级 `.env`：

```bash
# 从模板创建配置文件
copy .env.example .env        # Windows
# cp .env.example .env        # Linux/macOS
```

打开 `.env`，填入你的 API Key：

```env
# 至少配置一个 Provider（推荐 DeepSeek，性价比高）
DEEPSEEK_API_KEY=sk-your-key-here
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODELS=deepseek-chat,deepseek-reasoner

# 选择 Master 使用的模型
MODEL_MASTER=deepseek-chat
```

> 详细配置说明见 [.env.example](.env.example)，包含所有可选项和注释。

### 启动

```bash
# 直接运行
python cli.py

# 或使用全局命令（需先 pip install -e .）
astrea
```

---

## Skill 安装与管理

Skill 是按项目加载的 Markdown 指令片段，启动时注入 L1 上下文。当前实现只扫描运行项目目录下的 `.astrea/skills/`；未执行 `/trust` 时，运行项目目录是 `.astrea/workspace/`。

### 安装 Skill

先启动一次 ASTrea，让系统自动创建 `.astrea/` 运行目录；然后只需创建 Skill 子目录：

```text
.astrea/
└── skills/
    └── python-backend/
        ├── SKILL.md
        └── metadata.json
```

`SKILL.md`：

```markdown
# Python Backend Skill

当任务涉及 Python 后端开发时：
- 优先复用现有依赖和项目结构。
- 修改后运行最小可验证测试。
```

`metadata.json`：

```json
{
  "name": "python-backend",
  "description": "Python 后端开发约束",
  "version": "1.0",
  "tags": ["python", "backend"],
  "exclusive": [],
  "enabled": false
}
```

然后在 CLI 中执行：

```text
/skill reload
/skill enable python-backend
/skill
```

说明：
- 不带 `metadata.json` 时，可从 `SKILL.md` frontmatter 提取 `name` / `description`，且默认启用。
- 带 `metadata.json` 时建议显式配置 `enabled`。
- 会话中途切换 Skill 会重建 KV Cache；CLI 会提示是否先 `/compress`。
- 所有启用 Skill 总预算上限为 75,000 字符。

---

## MCP 安装与管理

MCP Server 在 `config/mcp_servers.json` 中声明。当前支持 `stdio` transport；启动后工具会自动桥接到 ToolRegistry，命名格式为 `mcp_<server>_<tool>`。

### 安装 MCP 依赖

```bash
pip install -r requirements.txt
```

如果 MCP Server 使用 `npx` 启动，还需要本机已安装 Node.js / npm。

### 配置 MCP Server

示例：

```json
[
  {
    "name": "context7",
    "command": "npx",
    "args": ["-y", "@upstash/context7-mcp@latest"],
    "transport": "stdio",
    "enabled": true
  }
]
```

配置字段：

| 字段 | 说明 |
|------|------|
| `name` | Server 名称，也是工具名前缀的一部分 |
| `command` | 启动命令，如 `npx`、`python`、`uvx` |
| `args` | 启动参数数组 |
| `transport` | 当前仅支持 `stdio` |
| `enabled` | `true` 时 CLI 启动后自动连接 |

### 管理 MCP

```text
/mcp
/mcp enable context7
/mcp disable context7
/mcp restart context7
```

说明：
- `/mcp enable|disable|restart` 是运行时操作，不会回写 `config/mcp_servers.json`。
- MCP 启动后新增工具会刷新到 Master 的工具清单。
- MCP 工具调用失败不会中断主流程，会以工具结果返回错误信息。

---

## 发布流程

本仓库使用 GitHub Actions + PyPI Trusted Publishing 发布，不需要在 GitHub Secrets 中保存长期 PyPI Token。

### Trusted Publisher 配置

TestPyPI：

```text
Owner: dfjmsf
Repository: ASTrea-AGENT
Workflow: publish.yml
Environment: testpypi
```

PyPI：

```text
Owner: dfjmsf
Repository: ASTrea-AGENT
Workflow: publish.yml
Environment: pypi
```

### 发布到 TestPyPI

在 GitHub Actions 页面手动运行 `Publish Python Package` workflow。手动运行只发布到 TestPyPI。

安装验证：

```bash
pipx install --python 3.11 --suffix testpypi --pip-args="--index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/" astrea-agent
```

### 发布到 PyPI

正式发布通过 Git tag 触发。Tag 必须等于 `pyproject.toml` 中的版本号并带 `v` 前缀。

```bash
git tag v0.7.1
git push origin v0.7.1
```

---

## CLI 命令

| 命令 | 说明 |
|------|------|
| `/help` | 查看所有可用命令 |
| `/exit` | 退出 ASTrea |
| `/model <name>` | 切换模型（支持补全） |
| `/thinking <off\|on\|max>` | 深度思考模式开关 |
| `/stats` | 查看 Token、步数、上下文等运行统计 |
| `/verbose <on\|off>` | 日志详细/紧凑模式 |
| `/todo` | 查看当前任务进度 |
| `/context` | 查看上下文空间使用情况 |
| `/compress` | 手动压缩 L2b 到 L4 归档 |
| `/mcp <enable\|disable\|restart> <server>` | MCP Server 管理 |
| `/skill <enable\|disable\|reload>` | Skill 管理 |
| `/config <init\|path>` | 初始化或查看用户级配置 |
| `/providers` | 查看已注册 LLM Provider |
| `/tools` | 查看已注册工具 |
| `/sessions [序号]` | 查看或恢复历史会话 |
| `/new` | 归档当前会话并开始新会话 |
| `/evo <on\|off\|status\|config>` | 管理自进化系统 |
| `/synth list` | 查看合成工具 |
| `/constraint list` | 查看约束规则 |

---

## 核心模块说明

### LayeredMemory

三层物理拼接的上下文管理：

| 层级 | 特性 | 内容 |
|------|------|------|
| **L1 锚点层** | 冻结前缀，永不变 | System Prompt + 项目规则 + Skill + 硬约束 |
| **L4 归档层** | 低频追加 | 压缩触发时写入归档摘要 |
| **L2b 对话链** | 只追加，状态机 | 当前目标 + 工具反馈 + 未完成事项 |

压缩策略：前 85% 消息按工具类型规则压缩为结构化 Markdown，保留最近 15% 原始消息。纯感知工具（read_file, grep）完全丢弃，状态变更工具保留骨架，写入工具保留完整描述。

### 自进化系统

**程序性记忆（工具合成）：** 轨迹采集 → 三层过滤（频率 → 熵 → 聚类）→ 模板填空式合成 → 四级验证（语法 → 签名 → 沙箱 → 元数据）→ 注册。

**反思性约束：** 失败采集 → 间隔复现检测 → 约束生成 → D/E 方案运行时注入。

**熵减淘汰：** 工具未使用 50 轮则删除，失败率 > 50% 则禁用；约束未触发 40 轮则删除，标签重叠 ≥ 80% 则合并。

### Subagent Fork

Master fork 完整 L1+L4+L2b 快照给 Subagent，Subagent 仅可使用白名单内的只读工具（grep, read_file, ls, run_command），执行结果以 `{conclusion, findings, suggestion}` JSON 报告回注 Master。支持 asyncio.gather 并发多个 Subagent。

---

## 技术栈

| 类别 | 技术 |
|------|------|
| 语言 | Python 3.10-3.12 |
| TUI | Rich + prompt_toolkit |
| LLM | OpenAI SDK（兼容 Qwen / DeepSeek / GPT） |
| 持久化 | SQLite |
| 外部工具 | MCP Protocol |
| 构建 | setuptools + pyproject.toml |

---

## License

Apache License 2.0
