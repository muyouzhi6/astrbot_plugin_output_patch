from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class Resp:
    type: str
    name: str
    chain_type: str | None = None

    @property
    def data(self):
        return types.SimpleNamespace(
            chain=types.SimpleNamespace(type=self.chain_type),
        )


class DummyLogger:
    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass


class DummyCleanReport:
    def has_removed(self):
        return False


class BaseAstrMessageEvent:
    pass


class BasePlatform:
    pass


class BaseMessageComponent:
    pass


class Plain(BaseMessageComponent):
    pass


class MessageChain:
    def __init__(self, chain=None):
        self.chain = chain or []

    def message(self, text):
        self.chain.append(text)
        return self


class ToolLoopAgentRunner:
    tool_schema_mode = "openai"
    streaming = False
    func_tool = None

    async def step(self):
        for item in self.sequence:
            yield item

    async def _iter_llm_responses(self):
        for item in self.iter_sequence:
            yield item

    def _func_tool_for_provider(self):
        return self.func_tool


def install_import_stubs() -> None:
    logger_module = types.ModuleType("astrbot.api")
    logger_module.logger = DummyLogger()

    components_module = types.ModuleType("astrbot.core.message.components")
    components_module.BaseMessageComponent = BaseMessageComponent
    components_module.Plain = Plain

    result_module = types.ModuleType("astrbot.core.message.message_event_result")
    result_module.MessageChain = MessageChain

    event_module = types.ModuleType("astrbot.core.platform.astr_message_event")
    event_module.AstrMessageEvent = BaseAstrMessageEvent

    platform_module = types.ModuleType("astrbot.core.platform.platform")
    platform_module.Platform = BasePlatform

    message_type_module = types.ModuleType("astrbot.core.platform.message_type")
    message_type_module.MessageType = types.SimpleNamespace()

    runner_module = types.ModuleType(
        "astrbot.core.agent.runners.tool_loop_agent_runner"
    )
    runner_module.ToolLoopAgentRunner = ToolLoopAgentRunner

    config_module = types.ModuleType("output_patch_under_test.config")
    config_module.PluginConfig = object

    sanitize_module = types.ModuleType("output_patch_under_test.sanitize")
    sanitize_module.collect_visible_text = lambda chain: ""
    sanitize_module.replace_in_chain = lambda chain, config: []
    sanitize_module.sanitize_chain = lambda chain, config: DummyCleanReport()

    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": logger_module,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.message": types.ModuleType("astrbot.core.message"),
        "astrbot.core.message.components": components_module,
        "astrbot.core.message.message_event_result": result_module,
        "astrbot.core.platform": types.ModuleType("astrbot.core.platform"),
        "astrbot.core.platform.astr_message_event": event_module,
        "astrbot.core.platform.platform": platform_module,
        "astrbot.core.platform.message_type": message_type_module,
        "astrbot.core.agent": types.ModuleType("astrbot.core.agent"),
        "astrbot.core.agent.runners": types.ModuleType("astrbot.core.agent.runners"),
        "astrbot.core.agent.runners.tool_loop_agent_runner": runner_module,
        "output_patch_under_test": types.ModuleType("output_patch_under_test"),
        "output_patch_under_test.config": config_module,
        "output_patch_under_test.sanitize": sanitize_module,
    }
    sys.modules.update(modules)


def load_runtime_hook(path: Path):
    install_import_stubs()
    spec = importlib.util.spec_from_file_location(
        "output_patch_under_test.runtime_hook",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def collect(
    sequence,
    *,
    mode="openai",
    runtime_hook_module,
    streaming: bool = False,
    func_tool=None,
):
    async def original_step(self):
        for item in self.sequence:
            yield item

    async def original_iter_llm_responses(self, *args, **kwargs):
        for item in getattr(self, "iter_sequence", []):
            yield item

    ToolLoopAgentRunner.step = original_step
    ToolLoopAgentRunner._iter_llm_responses = original_iter_llm_responses
    hook = runtime_hook_module.RuntimeOutputHook(config=None)
    hook._patch_tool_loop_agent_runner()

    runner = ToolLoopAgentRunner()
    runner.sequence = sequence
    runner.tool_schema_mode = mode
    runner.streaming = streaming
    runner.func_tool = func_tool
    return [item.name async for item in runner.step()]


async def collect_from_original(
    original_step,
    *,
    runtime_hook_module,
    streaming: bool = False,
    func_tool=None,
):
    async def original_iter_llm_responses(self, *args, **kwargs):
        for item in getattr(self, "iter_sequence", []):
            yield item

    ToolLoopAgentRunner.step = original_step
    ToolLoopAgentRunner._iter_llm_responses = original_iter_llm_responses
    hook = runtime_hook_module.RuntimeOutputHook(config=None)
    hook._patch_tool_loop_agent_runner()

    runner = ToolLoopAgentRunner()
    runner.streaming = streaming
    runner.func_tool = func_tool
    seen = []
    error = None
    try:
        async for item in runner.step():
            seen.append(item.name)
    except Exception as exc:
        error = exc
    return seen, error


async def collect_iter(
    *,
    runtime_hook_module,
    streaming: bool,
    func_tool,
    raises: bool = False,
):
    observations = []

    async def original_step(self):
        for item in getattr(self, "sequence", []):
            yield item

    async def original_iter_llm_responses(self, *args, **kwargs):
        observations.append(self.streaming)
        if raises:
            raise RuntimeError("provider failed")
        yield Resp("llm_response", "iter-result")

    ToolLoopAgentRunner.step = original_step
    ToolLoopAgentRunner._iter_llm_responses = original_iter_llm_responses
    hook = runtime_hook_module.RuntimeOutputHook(config=None)
    hook._patch_tool_loop_agent_runner()

    runner = ToolLoopAgentRunner()
    runner.streaming = streaming
    runner.func_tool = func_tool
    seen = []
    error = None
    try:
        async for item in runner._iter_llm_responses():
            seen.append(item.name)
    except Exception as exc:
        error = exc
    return seen, error, observations, runner.streaming


async def collect_step_state(
    *,
    runtime_hook_module,
    streaming: bool,
    func_tool,
    raises: bool = False,
):
    observations = []

    async def original_step(self):
        observations.append(self.streaming)
        if raises:
            raise RuntimeError("step failed")
        yield Resp("llm_result", "final")
        observations.append(self.streaming)

    async def original_iter_llm_responses(self, *args, **kwargs):
        yield Resp("llm_response", "iter-result")

    ToolLoopAgentRunner.step = original_step
    ToolLoopAgentRunner._iter_llm_responses = original_iter_llm_responses
    hook = runtime_hook_module.RuntimeOutputHook(config=None)
    hook._patch_tool_loop_agent_runner()

    runner = ToolLoopAgentRunner()
    runner.streaming = streaming
    runner.func_tool = func_tool
    seen = []
    consumer_observations = []
    error = None
    try:
        async for item in runner.step():
            seen.append(item.name)
            consumer_observations.append(runner.streaming)
    except Exception as exc:
        error = exc
    return seen, error, observations, consumer_observations, runner.streaming


async def collect_stream_timing(*, runtime_hook_module):
    events = []

    async def original_step(self):
        events.append("source:yield-d1")
        yield Resp("streaming_delta", "d1")
        events.append("source:after-d1")
        await asyncio.sleep(0)
        events.append("source:yield-d2")
        yield Resp("streaming_delta", "d2")
        events.append("source:after-d2")

    async def original_iter_llm_responses(self, *args, **kwargs):
        yield Resp("llm_response", "iter-result")

    ToolLoopAgentRunner.step = original_step
    ToolLoopAgentRunner._iter_llm_responses = original_iter_llm_responses
    hook = runtime_hook_module.RuntimeOutputHook(config=None)
    hook._patch_tool_loop_agent_runner()

    runner = ToolLoopAgentRunner()
    runner.streaming = True
    runner.func_tool = None
    async for item in runner.step():
        events.append(f"consumer:{item.name}")
    return events


async def main() -> None:
    default_path = Path(__file__).resolve().parents[1] / "core/runtime_hook.py"
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_path
    runtime_hook_module = load_runtime_hook(path.resolve())

    cases = [
        (
            "normal single llm_result",
            "openai",
            [Resp("llm_result", "final")],
            ["final"],
            {},
        ),
        (
            "openai multiple llm_result preserved",
            "openai",
            [Resp("llm_result", "first"), Resp("llm_result", "second")],
            ["first", "second"],
            {},
        ),
        (
            "tool_call drops llm preamble",
            "openai",
            [
                Resp("llm_result", "preamble"),
                Resp("tool_call", "call"),
                Resp("llm_result", "final"),
            ],
            ["call", "final"],
            {},
        ),
        (
            "tool_call_result drops llm preamble",
            "openai",
            [
                Resp("llm_result", "preamble"),
                Resp("tool_call_result", "result"),
                Resp("llm_result", "final"),
            ],
            ["result", "final"],
            {},
        ),
        (
            "normal streaming preserved",
            "openai",
            [Resp("streaming_delta", "delta-1"), Resp("streaming_delta", "delta-2")],
            ["delta-1", "delta-2"],
            {"streaming": True},
        ),
        (
            "streaming tool preamble dropped",
            "openai",
            [
                Resp("streaming_delta", "draft-1"),
                Resp("streaming_delta", "draft-2"),
                Resp("tool_call", "call"),
                Resp("streaming_delta", "final-delta"),
            ],
            ["call", "final-delta"],
            {"streaming": True, "func_tool": object()},
        ),
        (
            "skills_like non-stream fallback keeps last llm_result",
            "skills_like",
            [Resp("llm_result", "preamble"), Resp("llm_result", "fallback")],
            ["fallback"],
            {},
        ),
        (
            "skills_like streaming without tool never swallowed",
            "skills_like",
            [
                Resp("streaming_delta", "delta-1"),
                Resp("streaming_delta", "delta-2"),
                Resp("llm_result", "final"),
            ],
            ["delta-1", "delta-2", "final"],
            {"streaming": True},
        ),
    ]

    for name, mode, sequence, expected, kwargs in cases:
        actual = await collect(
            sequence,
            mode=mode,
            runtime_hook_module=runtime_hook_module,
            **kwargs,
        )
        if actual != expected:
            raise AssertionError(f"{name}: expected {expected}, got {actual}")

    async def normal_stream_then_error(self):
        yield Resp("streaming_delta", "delta-1")
        yield Resp("streaming_delta", "delta-2")
        raise RuntimeError("upstream failed")

    actual, error = await collect_from_original(
        normal_stream_then_error,
        runtime_hook_module=runtime_hook_module,
    )
    if actual != ["delta-1", "delta-2"] or not isinstance(error, RuntimeError):
        raise AssertionError(
            "normal streaming chunks should flush before re-raising upstream error: "
            f"actual={actual}, error={error!r}"
        )

    async def reasoning_stream_then_error(self):
        yield Resp("streaming_delta", "reasoning", chain_type="reasoning")
        raise RuntimeError("upstream failed")

    actual, error = await collect_from_original(
        reasoning_stream_then_error,
        runtime_hook_module=runtime_hook_module,
        streaming=True,
        func_tool=object(),
    )
    if actual != [] or not isinstance(error, RuntimeError):
        raise AssertionError(
            "reasoning chunks should stay suppressed on upstream error: "
            f"actual={actual}, error={error!r}"
        )

    async def llm_result_then_error(self):
        yield Resp("llm_result", "tool-preamble")
        raise RuntimeError("upstream failed")

    actual, error = await collect_from_original(
        llm_result_then_error,
        runtime_hook_module=runtime_hook_module,
        streaming=True,
        func_tool=object(),
    )
    if actual != [] or not isinstance(error, RuntimeError):
        raise AssertionError(
            "llm_result chunks must not flush on upstream error: "
            f"actual={actual}, error={error!r}"
        )

    async def mixed_visible_then_error(self):
        yield Resp("streaming_delta", "delta")
        yield Resp("llm_result", "tool-preamble")
        raise RuntimeError("upstream failed")

    actual, error = await collect_from_original(
        mixed_visible_then_error,
        runtime_hook_module=runtime_hook_module,
        streaming=True,
        func_tool=object(),
    )
    if actual != [] or not isinstance(error, RuntimeError):
        raise AssertionError(
            "mixed pending chunks must not flush on upstream error: "
            f"actual={actual}, error={error!r}"
        )

    actual, error, observations, restored_streaming = await collect_iter(
        runtime_hook_module=runtime_hook_module,
        streaming=True,
        func_tool=object(),
    )
    if (
        actual != ["iter-result"]
        or error is not None
        or observations != [False]
        or restored_streaming is not True
    ):
        raise AssertionError(
            "tool-capable requests should disable provider streaming and restore it: "
            f"actual={actual}, error={error!r}, observations={observations}, "
            f"restored={restored_streaming}"
        )

    actual, error, observations, restored_streaming = await collect_iter(
        runtime_hook_module=runtime_hook_module,
        streaming=True,
        func_tool=None,
    )
    if (
        actual != ["iter-result"]
        or error is not None
        or observations != [True]
        or restored_streaming is not True
    ):
        raise AssertionError(
            "tool-free requests should keep provider streaming enabled: "
            f"actual={actual}, error={error!r}, observations={observations}, "
            f"restored={restored_streaming}"
        )

    actual, error, observations, restored_streaming = await collect_iter(
        runtime_hook_module=runtime_hook_module,
        streaming=True,
        func_tool=object(),
        raises=True,
    )
    if (
        actual != []
        or not isinstance(error, RuntimeError)
        or observations != [False]
        or restored_streaming is not True
    ):
        raise AssertionError(
            "tool streaming guard should restore streaming after provider errors: "
            f"actual={actual}, error={error!r}, observations={observations}, "
            f"restored={restored_streaming}"
        )

    (
        actual,
        error,
        observations,
        consumer_observations,
        restored_streaming,
    ) = await collect_step_state(
        runtime_hook_module=runtime_hook_module,
        streaming=True,
        func_tool=object(),
    )
    if (
        actual != ["final"]
        or error is not None
        or observations != [False, False]
        or consumer_observations != [False]
        or restored_streaming is not True
    ):
        raise AssertionError(
            "tool-capable step should look non-streaming while yielded output is "
            "consumed, then restore streaming: "
            f"actual={actual}, error={error!r}, observations={observations}, "
            f"consumer={consumer_observations}, restored={restored_streaming}"
        )

    (
        actual,
        error,
        observations,
        consumer_observations,
        restored_streaming,
    ) = await collect_step_state(
        runtime_hook_module=runtime_hook_module,
        streaming=True,
        func_tool=None,
    )
    if (
        actual != ["final"]
        or error is not None
        or observations != [True, True]
        or consumer_observations != [True]
        or restored_streaming is not True
    ):
        raise AssertionError(
            "tool-free step should keep streaming enabled while yielded output is "
            "consumed: "
            f"actual={actual}, error={error!r}, observations={observations}, "
            f"consumer={consumer_observations}, restored={restored_streaming}"
        )

    (
        actual,
        error,
        observations,
        consumer_observations,
        restored_streaming,
    ) = await collect_step_state(
        runtime_hook_module=runtime_hook_module,
        streaming=True,
        func_tool=object(),
        raises=True,
    )
    if (
        actual != []
        or not isinstance(error, RuntimeError)
        or observations != [False]
        or consumer_observations != []
        or restored_streaming is not True
    ):
        raise AssertionError(
            "tool-capable step should restore streaming after step errors: "
            f"actual={actual}, error={error!r}, observations={observations}, "
            f"consumer={consumer_observations}, restored={restored_streaming}"
        )

    timing = await collect_stream_timing(runtime_hook_module=runtime_hook_module)
    expected_timing = [
        "source:yield-d1",
        "consumer:d1",
        "source:after-d1",
        "source:yield-d2",
        "consumer:d2",
        "source:after-d2",
    ]
    if timing != expected_timing:
        raise AssertionError(
            "tool-free streaming chunks must be yielded immediately: "
            f"expected={expected_timing}, got={timing}"
        )

    print(f"ok {len(cases) + 11} cases")


if __name__ == "__main__":
    asyncio.run(main())
