# LLM 配置详细说明

LLM 配置用于告诉 Storydex：本地 Agent 应该连接哪个模型服务、使用哪个 API Key、默认使用哪个模型，以及工具调用协议如何处理。新建提供方时可以直接选择常用服务商预设，Storydex 会填好协议类型和接口地址，并提供官方 API Key 页面入口。

配置文件通常位于：

```text
C:/Users/Septem/.storydex/.coomi/config/providers.json
```

## 什么时候需要配置

- 第一次安装 Storydex 后，还没有可用模型。
- 更换模型供应商、接口地址或 API Key。
- 标准模型和快速模型想分别使用不同模型。
- 工具调用异常，需要调整工具协议。
- 想把某个提供方设为当前默认提供方。

## 入口

1. 打开 Storydex。
2. 在右侧 Agent 面板顶部点击设置图标。
3. 打开 `LLM配置` 面板。

也可以从 Agent 面板顶部的模型/设置入口进入，最终都会打开同一个 LLM 配置窗口。

## 界面区域

| 区域 | 作用 |
| --- | --- |
| 顶部标题 `LLM配置` | 表示当前正在编辑 Coomi/Storydex 的模型提供方配置。 |
| `退出` | 关闭配置面板。关闭前如果没有保存，未保存内容不会可靠生效。 |
| `提供方` 下拉框 | 选择要查看或编辑的模型提供方。当前提供方会在名称后显示“当前”。 |
| `编辑提供方` | 当前选中的提供方详情。 |
| 底部操作区 | 显示配置文件路径，并提供保存、应用等操作。 |

## 顶部和底部按钮

| 按钮/图标 | 位置 | 作用 | 使用建议 |
| --- | --- | --- | --- |
| `退出` | 面板右上角 | 关闭 LLM 配置面板。 | 改完配置后先点 `保存` 或 `应用`，再退出。 |
| `+` / `新建提供方` | 提供方下拉框右侧 | 从常用服务商预设或自定义接口新增配置。 | 优先选择预设，协议类型和接口地址会自动填写。 |
| 删除图标 / `删除提供方` | `+` 右侧 | 删除当前选中的提供方。 | 删除前确认不是当前正在使用的配置；删除后需要保存。 |
| `保存` | 面板底部 | 保存 providers.json，但不一定立即切换运行时。 | 批量编辑多个配置时先保存。 |
| `应用` | 面板底部 | 保存、切换到当前正在编辑的提供方，并让运行中的 Agent 配置生效。 | 改 API Key、模型名或提供方后优先点 `应用`。 |

键盘快捷键：

| 快捷键 | 作用 |
| --- | --- |
| `Ctrl+S` / `Cmd+S` | 保存当前 LLM 配置。 |

## 字段说明

| 字段 | 作用 | 示例 | 注意事项 |
| --- | --- | --- | --- |
| `提供方 ID` | 供应商配置的唯一标识。 | `opencode-go`、`openai-main` | 建议使用英文、数字、短横线；不要频繁修改，避免旧配置引用失效。 |
| `类型` | 提供方使用的 API 协议。 | `OpenAI Compatible`、`OpenAI Responses`、`Anthropic Messages` | 预设会自动选择；只有自定义接口需要按服务商文档判断。旧 `generic/openai/anthropic` 配置会自动转换。 |
| `工具协议` | Agent 调用工具时采用的协议。 | `auto`、`native`、`structured`、`mimo`、`disabled` | 不确定时使用 `auto`；如果模型不支持工具调用，可临时使用 `disabled`。 |
| `显示名称` | 界面上展示给用户看的名称。 | `deepseek-v4-pro` | 不影响接口调用，只用于识别。 |
| `接口地址` | 模型 API 的 base URL。 | `https://example.com/v1` | OpenAI 兼容接口通常以 `/v1` 结尾。 |
| `API 密钥` | 调用模型服务需要的密钥。 | `sk-...` | 不要在截图、日志或公开文档中泄露真实密钥。 |
| `标准模型` | 复杂任务、长文生成、规划任务优先使用的模型。 | `deepseek-v4-pro` | 建议选择质量更高、上下文更稳的模型。 |
| `快速模型` | 简短任务、轻量判断、快速响应可使用的模型。 | `deepseek-v4-flash` | 可选择速度快、成本低的模型。 |
| `上下文窗口（tokens）` | 告诉 Storydex 当前模型的真实上下文窗口大小，Agent 的自动压缩阈值按它计算。 | `128000` | 按模型服务商标称的上下文长度填写；留空使用默认值。使用小上下文模型时建议如实填写，避免长对话溢出。 |

## 工具协议怎么选

| 协议 | 适用情况 |
| --- | --- |
| `auto` | 默认推荐。Storydex/Coomi 自动判断可用协议。 |
| `native` | 模型服务原生支持工具调用时使用。 |
| `structured` | 需要结构化工具调用，但服务商不完全支持原生工具协议时尝试。 |
| `mimo` | 兼容特定服务商或中间层的工具协议。 |
| `disabled` | 关闭工具调用，只让模型回答文本；排查工具调用问题时可临时使用。 |

如果出现“模型能聊天，但不能写文件/不能读项目/工具调用失败”，优先检查 `工具协议` 是否适合当前服务商。

## 常用操作

### 从预设新增模型提供方

1. 打开 `LLM配置`。
2. 点击 `+`。
3. 选择服务商；第一次配置且列表为空时，直接在空状态下选择服务商。
4. 在 `API 密钥` 右侧点击 `获取密钥`，系统浏览器会打开该服务商的官方页面。
5. 创建并复制 API Key，粘贴回 Storydex。
6. 点击 `获取模型` 后选择模型，或者直接在 `标准模型` 中输入服务商提供的模型 ID。
7. `快速模型` 可留空，也可以继续选择或手动输入另一个模型 ID。
8. 点击 `应用`，保存并切换到这个提供方。

预设不会锁定模型名。不同账号、套餐和区域可见的模型可能不同，因此 `标准模型` 和 `快速模型` 始终保留自由输入能力。

### 当前内置预设

| 服务商 | 自动填写的协议 | 自动填写的接口地址 |
| --- | --- | --- |
| OpenAI | OpenAI Responses | `https://api.openai.com/v1` |
| Anthropic | Anthropic Messages | `https://api.anthropic.com` |
| DeepSeek | OpenAI Compatible | `https://api.deepseek.com/v1` |
| Kimi | OpenAI Compatible | `https://api.moonshot.cn/v1` |
| 智谱 GLM | OpenAI Compatible | `https://open.bigmodel.cn/api/paas/v4` |
| 阿里云百炼 | OpenAI Compatible | `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| MiniMax | OpenAI Compatible | `https://api.minimaxi.com/v1` |
| 硅基流动 | OpenAI Compatible | `https://api.siliconflow.cn/v1` |
| OpenRouter | OpenAI Compatible | `https://openrouter.ai/api/v1` |
| Google Gemini | OpenAI Compatible | `https://generativelanguage.googleapis.com/v1beta/openai` |
| xAI | OpenAI Compatible | `https://api.x.ai/v1` |

服务商接口和账号权限可能变化。如果预设地址不适用于你的套餐，可以直接修改接口地址，或从 `+` 菜单选择 `自定义接口`。

### 新增自定义接口

1. 点击 `+`，选择 `自定义接口`。
2. 填写 `提供方 ID`、显示名称和接口地址。
3. 按上游实际协议选择类型，工具协议不确定时保持 `auto`。
4. 粘贴 API Key，并获取或手动输入模型 ID。
5. 点击 `应用`。

### 更换 API Key

1. 在 `提供方` 下拉框选择要修改的提供方。
2. 修改 `API 密钥`。
3. 点击 `应用`。
4. 回到 Agent 面板发送一句简单请求，例如“请回复：配置测试成功”。

### 切换默认模型提供方

1. 在 `提供方` 下拉框选择目标提供方。
2. 点击 `应用`。
3. Agent 面板底部的模型状态会更新为当前配置。

### 删除不用的提供方

1. 在 `提供方` 下拉框选择要删除的提供方。
2. 点击删除图标。
3. 点击 `保存`。

删除前建议确认它不是唯一可用提供方。

## 和 Agent 面板的关系

LLM 配置只负责“模型从哪里来”。Agent 面板负责“本轮任务怎么执行”。

Agent 面板中常见状态包括：

| 项目 | 说明 |
| --- | --- |
| 当前模型 | 显示当前正在使用的模型名。 |
| 权限模式 | 控制 Agent 是否能读取、修改项目文件。 |
| 推理模式 | 控制本轮任务的推理强度或规划倾向。 |
| `片段数量` | 剧情生成时本轮生成几条片段，默认 1。 |
| `章节篇幅（实验）` | 默认关闭；仅项目显式启用篇幅档位实验开关时显示。 |
| `章节模板` | 新故事或需要新章节结构时使用的章节目录模板。 |

### 推理强度如何生效

推理强度默认使用模型接口提供的原生参数。新会话初始选择 `高`；加载当前模型能力后，如果该模型没有声明 `高`，界面会切回 `自动`。切换后，普通执行、重试、重新执行和排队续发都会使用当前档位。选择器只展示 `自动` 和当前模型能力对象声明的档位，并显示用户档位与实际发送的 wire 字段，避免把两者混为一谈。

| 面板档位 | 行为 |
| --- | --- |
| `自动` | 不发送任何推理强度参数，使用模型或网关自己的动态默认值。 |
| `低` | 优先降低延迟和推理 token 消耗。 |
| `中` | 在响应速度和推理深度之间平衡。 |
| `高` | 默认值，偏向充分推理。 |
| `超高` | 使用模型声明的 `xhigh` 档位；它不一定是该模型的最高档。 |
| `最大` | 使用模型声明的最高用户档位 `max`。 |

Storydex 会在运行时按提供方协议转换：

| 提供方类型 | 请求参数 |
| --- | --- |
| `OpenAI Compatible` | 通常使用 `reasoning_effort`。 |
| `OpenRouter` | 使用 `reasoning.effort`。 |
| `OpenAI Responses` | `reasoning.effort` |
| `Anthropic Messages` | 新模型使用 adaptive thinking 和 `output_config.effort`；旧模型使用 `thinking.budget_tokens`，其中 Opus 4.5 同时使用 effort。 |
| `Gemini Native` | Gemini 3 及以后使用 `thinkingLevel`；Gemini 2.5 使用 `thinkingBudget`。 |

档位会按具体模型和路由转换，不再按“是不是 GPT”做二分判断。当前内置规则包括：

| 模型/路由 | 默认展示档位 |
| --- | --- |
| GPT-5 系列（OpenAI Compatible / Responses） | `low / medium / high / xhigh / max` |
| Claude Opus 4.6、Sonnet 4.6 | `low / medium / high / max` |
| Claude Opus 4.7+、Opus 5、Sonnet 5、Fable 5 | `low / medium / high / xhigh / max` |
| DeepSeek V4（官方接口） | `high / max` |
| DeepSeek V4（OPENCODE，含 `v4flash...` 别名） | `low / high / max`；`max` 标记为路由敏感 |
| DeepSeek V4（OpenRouter） | `high / xhigh` |
| Kimi K3（Moonshot 官方或 Anthropic Messages） | `low / high / max` |
| Kimi K3（OPENCODE） | `max` |

以上是“内置已知能力”，不是对所有同名中转的保证。同一个模型名换到未知 base URL 后，Storydex 会采用更保守的公共子集；中转明确支持其他档位时，应使用模型级 profile 或映射声明。模型没有声明的档位不会显示，也不会在调用时静默降级；异常请求会直接报错，避免界面选择与实际发送不一致。

Storydex 的标准用户档位到 `max` 为止。`ultra` 属于特定 Codex 编排工作流的扩展语义，不进入通用 Provider 能力和请求协议。

`supports_reasoning_effort` 是三态配置：未配置时，Storydex 根据协议和模型名判断是否支持；设为 `true` 时强制发送；设为 `false` 时不发送原生强度字段。这样普通 GPT-4、本地模型或只实现了基础 Chat Completions 的网关不会因为未知字段而报错。部分 Ollama、vLLM、LM Studio 或自建兼容服务不接受推理强度参数，这种情况下可手动在对应提供方配置中加入：

```json
{
  "supports_reasoning_effort": false
}
```

如果兼容网关支持推理强度但使用不同的档位名称，可添加显式映射。例如把 Storydex 的 `超高` 转成上游的 `max`：

```json
{
  "reasoning_effort_map": {
    "xhigh": "max"
  }
}
```

非空的 `reasoning_effort_map` 也会显式启用原生推理参数，并声明对应的用户档位。例如配置 `"max": "max"` 后，非 GPT 模型也会出现 `最大`；它不依赖模型名称猜测。

同一提供方有多个能力不同的模型时，建议使用模型级 profile，而不是让一个 Provider 级布尔值覆盖所有模型：

```json
{
  "reasoning_profiles": {
    "gpt-5.6-luna": {
      "supported": true,
      "levels": ["low", "medium", "high", "xhigh", "max"]
    },
    "kimi-k3": {
      "supported": true,
      "levels": ["low", "high", "max"],
      "effort_map": {"max": "max"}
    },
    "*": {
      "supported": false
    }
  }
}
```

解析优先级是“精确模型 profile -> `*` profile -> Provider 级配置 -> 模型规则 -> unknown”。能力未知或明确不支持时，显式档位默认不可选，`自动` 仍可用且不会发送强度字段。

`ReasoningPlan` 和 `ModelCompleted` 会记录实际请求字段、响应模型及 reasoning token 等诊断信息，但只进入 Trace 和落盘日志，不会作为两条额外消息显示在主对话中。

推理强度采用“严格配置校验、运行时隔离”的双轨策略：配置检查仍会指出不合法的档位或映射；实际模型请求遇到过期能力缓存、未声明档位、非法 token 映射等问题时，会回到 `自动`（或已显式开启的提示词 fallback）继续主请求，并在 `ReasoningPlan.fallbackReason` 中记录原因。单个坏档位也不会让其他 Provider、模型或合法档位从能力列表中消失。

如果上游以 HTTP `400/422` 明确拒绝 reasoning/thinking 字段，Storydex 会仅移除对应推理字段后重试；非推理相关的参数错误不会被静默重试。OpenAI Compatible 网关同时要求补充消息 ID 时，两种兼容回退可以按需组合；流式连接后续若发生传输重试，也会复用已经协商成功的请求体，避免反复发送已被拒绝的推理字段。Anthropic 的 interleaved-thinking beta 头只会在实际发送旧式 `thinking.type=enabled` 且存在工具调用时附加，回退到普通请求后不会保留。

对没有原生强度字段、但希望用提示词做软控制的模型，可以显式开启：

```json
{
  "supports_reasoning_effort": false,
  "reasoning_prompt_fallback": true
}
```

提示词 fallback 默认关闭。开启后界面会标记“仅提示词控制，未发送原生字段”；它只能表达本地指导意图，不能证明模型原生支持该档位，也不能证明上游按对应强度执行。`supports_reasoning_effort: false` 本身不会修改提示词或关闭模型固有的推理行为。

## 注意事项

- Storydex 的 Agent 基座是 Coomi；Coomi 负责通用 Agent 能力，Storydex 负责小说项目编排。
- LLM 配置属于本机用户配置，不是小说项目内容。
- API Key 是敏感信息，不要写入小说正文、角色文件、WIKI 或公开仓库。
- 如果当前配置不可用，知识图谱等模块可能会退回本地解析模式，能展示基础信息，但深度生成能力会受限。

## 可以这样问 Agent

```text
请检查当前 LLM 配置是否能正常调用，并用一句话回复测试结果。
```

```text
如果我想用 OpenAI 兼容接口，LLM 配置里每个字段应该怎么填写？
```

```text
当前模型不能调用工具，可能需要调整哪个配置？
```

## 常见问题

### 保存和应用有什么区别？

`保存` 主要把配置写入 providers.json；`应用` 会让当前运行中的 Agent 尽量立即使用新配置。改完当前提供方、API Key 或模型名后，建议直接点 `应用`。

### 标准模型和快速模型可以一样吗？

可以。刚开始使用时可以填同一个模型，后续再根据成本和速度拆分。

### 为什么填了 API Key 还是失败？

常见原因有：接口地址不正确、模型名不存在、服务商不兼容当前工具协议、API Key 没有额度或权限、网络不可用。

`获取模型` 会按当前类型选择认证方式：Anthropic Messages 使用 Anthropic 原生的 `x-api-key`，其他预设使用 Bearer 认证。如果服务商没有提供兼容的模型列表接口，界面会提示获取失败；这不影响直接在模型输入框中填写模型 ID。

### providers.json 可以手动编辑吗？

可以，但建议优先使用界面编辑。手动编辑后需要重新打开 LLM 配置或点击应用，让 Storydex 重新读取配置。
