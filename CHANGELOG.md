v2.1.19

提前加固 AstrBot tool-loop 原始 LLM response, 防止内部状态在进入发送链路, history 或记忆前泄露.

Bug Fixes:

- 在 `ToolLoopAgentRunner._iter_llm_responses` 产出前清洗 `result_chain`, `completion_text` 和 `reasoning_content`, 避免 `content+tool_calls` 混合响应把工具前导语或内部状态写入可见结果.
- 移除 `my_mood: ...`, `Warner: ...`, `SYSTEM NOTICE`, JSON `tool_calls` / `reasoning_content` 等泄露形态.
- 收窄状态行正则, 支持真实换行和字面量 `\n`, 删除状态行时保留后续正常正文.
- 增加 runner hook 和 sanitizer 回归测试, 覆盖 raw response 预清洗和 `my_mood` / `Warner` 泄露样本.

v2.1.18

加固最终出站清洗, 防止思维链, provider 调试对象和内部状态块进入可见对话.

Bug Fixes:

- 过滤未闭合的 `&&think` / `<think>` 思维链片段, 避免模型把推理草稿当正文发出.
- 拦截 `ChatCompletion(...)`, `tool_calls=...`, `function_call=...`, `reasoning_content=...` 等 provider 调试结构文本.
- 移除 `[Current mood: ...]`, `(Wear: ...)`, `(Current status: ...)` 这类内部状态块, 同时保留正常可见回复.
- 增加 sanitize 回归测试, 覆盖私有输出删除和普通“工具调用”技术讨论不误杀.

v2.1.17

加固 AstrBot tool loop 输出链路, 防止工具调用前导语和 reasoning 片段外泄.

Bug Fixes:

- 在 `ToolLoopAgentRunner` 层拦截工具调用轮次, 丢弃工具调用前的可见 LLM 草稿.
- 工具可用时临时关闭 provider streaming, 等最终 `tools_call_name` 落定后再决定输出.
- 保持无工具请求的正常流式输出时序, 避免把普通回复攒到末尾批量发送.
- 增加 runner hook 回归测试, 覆盖工具前导语丢弃, 异常恢复和流式时序.

v2.1.16

完成自用补丁身份迁移, 避免继续命中上游插件市场更新源.

Maintenance:

- 将插件内部名称从 `astrbot_plugin_outputpro` 改为 `astrbot_plugin_output_patch`.
- 将默认插件目录和插件数据目录切换到 `astrbot_plugin_output_patch`.
- 更新文档中的迁移说明, 旧配置需要复制为 `astrbot_plugin_output_patch_config.json`.

v2.1.15

将 fork 转为自用 AstrBot 输出补丁版本.

Maintenance:

- 将插件展示名改为“输出补丁”.
- 更新插件描述, 作者和仓库地址, 明确这是自用补丁版本.
- 保留插件内部名称 `astrbot_plugin_outputpro`, 继续兼容既有 AstrBot 配置文件和插件数据目录.

v2.1.14

从上游同步低风险修复，并修正 fork 更新源。

Bug Fixes:

- 修复 QQ 合并转发节点在多机器人/多账号场景下可能使用错误昵称或头像的问题。
- 修复 Telegram 折叠引用只识别话题群 `chat_id#thread_id`，普通聊天无法发送折叠内容的问题。
- 优化报错转发内容，追加来源群、用户、原始输入、命中关键词和报错原文，同时避免污染 `ctx.plain`。

Maintenance:

- 将插件元数据更新源固定为 `muyouzhi6/astrbot_plugin_outputpro/tree/main`，避免 AstrBot 更新器默认回退到不存在的 `master.zip`。

v2.1.7

为 Telegram 平台新增长消息和自动撤回支持，扩展 TTS 以支持可插拔模型提供方和跨平台 QQ 语音中继，并优化消息拆分、空格保护以及智能回复行为。

New Features:

- 在转发步骤中，通过可展开的引用块支持 Telegram 长消息处理。
- 支持可配置的 TTS 模型提供方，并在需要时回退到 QQ 语音聊天中继，包括跨平台语音中继能力。

Bug Fixes:

- 确保在拆分步骤中丢弃前导的空内容，同时避免产生空的最终结果。
- 修复智能回复队列处理，通过对字符串化的消息 ID 进行操作并避免清空整个队列。

Enhancements:

- 改进 TTS 行为，将音频记录构造集中处理，并支持多种音频 URL/文件格式。
- 在消息拆分时规范化和清理前导空片段，同时保留提及（@）和回复信息。
- 调整空格保护逻辑，将仅包含 CJK 的空格视为普通空格，并保护其他空格模式。
- 增强智能回复触发逻辑，以支持更多消息组件类型，并跟踪机器人回复标记，以便更好地插入引用。
