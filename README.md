# agent-review

一个代理技能：让多个模型后端独立审核同一范围，再由一个汇总模型合并去重，生成 Markdown 报告。

`SKILL.md` 是技能定义，代理依据其中的规则调用执行入口 `agent-review.sh`（Bash + Python 3 标准库实现）。审核与汇总都通过各家的命令行程序完成；模型只输出自由正文，脚本不解析结构化结果。"完成"只表示进程正常退出且有正文，不代表覆盖了全部范围。

## 安装

将整个目录放到代理的技能目录，目录名保持 `agent-review`（与 `SKILL.md` 的 `name` 一致）：

```bash
git clone <仓库地址> ~/.agents/skills/agent-review
# 或复制本目录到以下任一位置：
#   全局：~/.agents/skills/agent-review
#   项目：<项目>/.agents/skills/agent-review
```

然后创建本机模型配置：

```bash
cp models.conf.example models.conf
```

## 调用方式

在对话中使用准确英文名称 `agent-review`（或 `$agent-review`）明确要求调用，例如"使用 `agent-review` 审核最近一次提交"。中文名称、译名或"多模型审核"等描述不满足调用条件。

调用时可通过自然语言向代理说明以下选项，代理按 `SKILL.md` 的规则映射为脚本参数：

| 需求 | 对应参数 | 默认 |
|---|---|---|
| 指定审核模型（短名称，空格分隔） | `--review-models "a b"` | 全部模型 |
| 指定汇总模型 | `--summarizer-model 短名称` | `[summarizer]` 小节 |
| 每个模型的审核预算 | `--review-timeout`（`s`/`m`/`h`） | `2000s`，最多 2 次尝试 |
| 单次汇总时间上限 | `--summary-timeout` | `1200s`，最多 3 次尝试 |
| 指定报告路径 | 第二个位置参数 | 本次运行临时目录下的 `report.md` |
| 列出可用模型 | `--list-models` | — |

## 依赖

- Bash、Python 3（只用标准库）
- 按 `models.conf` 中各模型 `backend` 的值安装对应命令行程序：

| backend | 命令行程序 | 说明 |
|---|---|---|
| `opencode`（默认） | `opencode` | 以 `--format json` 事件流读取正文 |
| `codex` | `codex` | `codex exec`，只读沙箱 |
| `kimi` | `kimi` | `stream-json` 输出 |
| `cursor` | `cursor-agent` | `stream-json` 输出 |
| `agycli` | `agy` | 纯文本输出，`--print-timeout` 跟随剩余预算 |
| `devin` | `devin` | `-p` 非交互模式，纯文本输出 |

## 配置

`models.conf` 每个小节定义一个模型，小节名即 `--review-models` 使用的短名称：

- `model`：模型 ID，原样传给后端 CLI 的 `--model` 参数。
- `backend`：上表中的后端名，省略时为 `opencode`。
- `[summarizer]` 小节：默认汇总模型，填其他小节的短名称。

`models.conf` 为本机配置，已被 `.gitignore` 忽略；以 `models.conf.example` 为模板。

## 运行结果

每次运行创建独立临时目录 `${TMPDIR:-/tmp}/agent-review-*`，包含：

- `run.json`：运行状态、各模型每次尝试的退出码与正文字数。
- `run.log`：运行日志。
- `models/<编号>/answer.md`：各模型的审核正文；`attempt-N/` 下是当次尝试的原始 stdout/stderr。
- `summary/`：汇总尝试与最终报告正文。

报告文件只含汇总正文，不含运行信息。退出码：

| 退出码 | 状态 | 含义 |
|---|---|---|
| `0` | `complete` | 所有模型正常退出且有正文，汇总成功 |
| `0` | `partial` | 至少一个模型完成，其余失败、超时或无正文；汇总成功 |
| `1` | `failed` | 没有模型完整完成，或汇总失败 |
| `130` | `interrupted` | 运行被中断，已有结果保留 |

## 测试

```bash
cd /path/to/agent-review
python3 -m unittest tests.test_agent_review -v
```

所有模型命令由 `tests/mock_cli.py` 模拟，不需要真实后端和网络。
