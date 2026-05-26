from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from functools import wraps
from typing import Any

from astrbot.api import logger
from astrbot.core.message.components import BaseMessageComponent, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.platform import Platform
from astrbot.core.platform.message_type import MessageType

from .config import PluginConfig
from .sanitize import collect_visible_text, replace_in_chain, sanitize_chain


@dataclass(slots=True)
class FilterDecision:
    action: str = "pass"
    matched_word: str | None = None


def _iter_subclasses(cls: type) -> list[type]:
    seen: set[type] = set()
    pending = [cls]
    ordered: list[type] = []

    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        ordered.append(current)
        pending.extend(current.__subclasses__())

    return ordered


def _normalize_message(message: Any) -> MessageChain | None:
    if message is None:
        return None
    if isinstance(message, MessageChain):
        return message
    if isinstance(message, str):
        return MessageChain().message(message)
    if isinstance(message, list) and all(
        isinstance(component, BaseMessageComponent) for component in message
    ):
        return MessageChain(chain=message)
    return None


def _is_reasoning_response(item: Any) -> bool:
    chain = getattr(getattr(item, "data", None), "chain", None)
    return getattr(chain, "type", None) == "reasoning"


def _can_flush_after_upstream_error(pending_visible: list[Any]) -> bool:
    return bool(pending_visible) and all(
        getattr(item, "type", None) == "streaming_delta" for item in pending_visible
    )


def _has_provider_tools(runner_self: Any) -> bool:
    func_tool_for_provider = getattr(runner_self, "_func_tool_for_provider", None)
    if callable(func_tool_for_provider):
        try:
            return func_tool_for_provider() is not None
        except Exception:
            logger.warning(
                "[outputpro:ToolLoopAgentRunner._iter_llm_responses] failed to "
                "inspect provider tools; disable streaming defensively",
                exc_info=True,
            )
            return True

    req = getattr(runner_self, "req", None)
    return getattr(req, "func_tool", None) is not None


async def _flush_pending_visible(
    pending_visible: list[Any],
    *,
    keep_only_last: bool = False,
    drop_reasoning: bool = False,
):
    if not pending_visible:
        return

    if keep_only_last and len(pending_visible) > 1:
        # In skills-like fallback, the first llm_result can be the discarded
        # tool-call preamble and the last one is the real assistant fallback.
        items = [pending_visible[-1]]
    else:
        items = list(pending_visible)

    if drop_reasoning:
        items = [item for item in items if not _is_reasoning_response(item)]

    pending_visible.clear()
    for item in items:
        yield item


class RuntimeOutputHook:
    def __init__(self, config: PluginConfig):
        self.config = config
        self._patched: list[tuple[type, str, Any]] = []
        self._installed = False

    def install(self) -> None:
        for cls in _iter_subclasses(AstrMessageEvent)[1:]:
            self._patch_event_send(cls)
            self._patch_event_streaming(cls)

        for cls in _iter_subclasses(Platform)[1:]:
            self._patch_platform_send_by_session(cls)

        self._patch_tool_loop_agent_runner()

        self._installed = True

    def uninstall(self) -> None:
        for cls, attr, original in reversed(self._patched):
            setattr(cls, attr, original)
        self._patched.clear()
        self._installed = False

    def _patch(self, cls: type, attr: str, wrapper_factory) -> None:
        original = getattr(cls, attr, None)
        if original is None or getattr(original, "__outputpro_wrapped__", False):
            return

        wrapped = wrapper_factory(original)
        wrapped.__outputpro_wrapped__ = True
        self._patched.append((cls, attr, original))
        setattr(cls, attr, wrapped)

    def _patch_event_send(self, cls: type) -> None:
        def factory(original):
            hook = self

            @wraps(original)
            async def wrapped(event_self, message, *args, **kwargs):
                chain = _normalize_message(message)
                if chain is not None:
                    decision = await hook._filter_chain(
                        chain,
                        source=f"{cls.__name__}.send",
                        session=getattr(event_self, "session", None),
                    )
                    if decision.action == "drop":
                        return None
                    message = chain
                return await original(event_self, message, *args, **kwargs)

            return wrapped

        self._patch(cls, "send", factory)

    def _patch_event_streaming(self, cls: type) -> None:
        def factory(original):
            hook = self

            @wraps(original)
            async def wrapped(event_self, generator, *args, **kwargs):
                if generator is None:
                    return await original(event_self, generator, *args, **kwargs)

                async def filtered_generator() -> AsyncGenerator[Any, None]:
                    emitted_error_replacement = False
                    async for item in generator:
                        chain = _normalize_message(item)
                        if chain is None:
                            yield item
                            continue

                        decision = await hook._filter_chain(
                            chain,
                            source=f"{cls.__name__}.send_streaming",
                            session=getattr(event_self, "session", None),
                        )
                        if decision.action == "drop":
                            continue
                        if decision.action == "replaced":
                            if emitted_error_replacement:
                                continue
                            emitted_error_replacement = True

                        if not chain.chain:
                            continue

                        yield chain

                return await original(event_self, filtered_generator(), *args, **kwargs)

            return wrapped

        self._patch(cls, "send_streaming", factory)

    def _patch_platform_send_by_session(self, cls: type) -> None:
        def factory(original):
            hook = self

            @wraps(original)
            async def wrapped(platform_self, session, message_chain, *args, **kwargs):
                chain = _normalize_message(message_chain)
                if chain is not None:
                    decision = await hook._filter_chain(
                        chain,
                        source=f"{cls.__name__}.send_by_session",
                        session=session,
                    )
                    if decision.action == "drop":
                        return None
                    message_chain = chain
                return await original(
                    platform_self,
                    session,
                    message_chain,
                    *args,
                    **kwargs,
                )

            return wrapped

        self._patch(cls, "send_by_session", factory)

    def _patch_tool_loop_agent_runner(self) -> None:
        try:
            from astrbot.core.agent.runners.tool_loop_agent_runner import (
                ToolLoopAgentRunner,
            )
        except Exception:
            logger.warning(
                "[outputpro:runtime_hook] ToolLoopAgentRunner unavailable; "
                "skip tool-loop output hardening",
                exc_info=True,
            )
            return

        def iter_factory(original):
            @wraps(original)
            async def wrapped(runner_self, *args, **kwargs):
                original_streaming = bool(getattr(runner_self, "streaming", False))
                disabled_streaming = original_streaming and _has_provider_tools(
                    runner_self
                )

                if disabled_streaming:
                    runner_self.streaming = False
                    logger.debug(
                        "[outputpro:ToolLoopAgentRunner._iter_llm_responses] "
                        "disabled provider streaming for tool-capable request"
                    )

                try:
                    async for resp in original(runner_self, *args, **kwargs):
                        yield resp
                finally:
                    if disabled_streaming:
                        runner_self.streaming = original_streaming

            wrapped.__outputpro_tool_stream_guard__ = True
            return wrapped

        def step_factory(original):
            @wraps(original)
            async def wrapped(runner_self, *args, **kwargs):
                original_streaming = bool(getattr(runner_self, "streaming", False))
                disabled_streaming = original_streaming and _has_provider_tools(
                    runner_self
                )
                if disabled_streaming:
                    runner_self.streaming = False
                    logger.debug(
                        "[outputpro:ToolLoopAgentRunner.step] disabled agent "
                        "streaming for tool-capable request"
                    )

                pending_visible: list[Any] = []
                dropped_before_tool_call = 0

                try:
                    try:
                        async for resp in original(runner_self, *args, **kwargs):
                            resp_type = getattr(resp, "type", None)
                            if (
                                resp_type == "streaming_delta"
                                and not disabled_streaming
                            ):
                                yield resp
                                continue

                            if resp_type in ("llm_result", "streaming_delta"):
                                pending_visible.append(resp)
                                continue

                            if (
                                isinstance(resp_type, str)
                                and resp_type.startswith("tool_call")
                            ):
                                dropped_before_tool_call += len(pending_visible)
                                pending_visible.clear()
                                yield resp
                                continue

                            async for pending in _flush_pending_visible(pending_visible):
                                yield pending
                            yield resp
                    except Exception:
                        pending_count = len(pending_visible)
                        reasoning_count = sum(
                            1 for item in pending_visible if _is_reasoning_response(item)
                        )
                        can_flush = _can_flush_after_upstream_error(pending_visible)
                        if can_flush:
                            async for pending in _flush_pending_visible(
                                pending_visible,
                                drop_reasoning=True,
                            ):
                                yield pending
                        else:
                            pending_visible.clear()
                        flushed_count = (
                            pending_count - reasoning_count if can_flush else 0
                        )
                        if pending_count:
                            logger.warning(
                                "[outputpro:ToolLoopAgentRunner.step] flushed %d safe "
                                "streaming chunk(s) after upstream error; dropped %d "
                                "ambiguous/reasoning chunk(s)",
                                flushed_count,
                                pending_count - flushed_count,
                            )
                        raise

                    keep_only_last = (
                        getattr(runner_self, "tool_schema_mode", None) == "skills_like"
                        and len(pending_visible) > 1
                        and all(
                            getattr(item, "type", None) == "llm_result"
                            for item in pending_visible
                        )
                    )
                    async for pending in _flush_pending_visible(
                        pending_visible,
                        keep_only_last=keep_only_last,
                    ):
                        yield pending

                    if dropped_before_tool_call:
                        logger.info(
                            "[outputpro:ToolLoopAgentRunner.step] suppressed %d visible "
                            "LLM chunk(s) before tool call",
                            dropped_before_tool_call,
                        )
                finally:
                    if disabled_streaming:
                        runner_self.streaming = original_streaming

            wrapped.__outputpro_tool_loop_preamble_filter__ = True
            return wrapped

        already_stream_guard_installed = bool(
            getattr(
                getattr(ToolLoopAgentRunner, "_iter_llm_responses", None),
                "__outputpro_tool_stream_guard__",
                False,
            )
        )
        already_installed = bool(
            getattr(
                getattr(ToolLoopAgentRunner, "step", None),
                "__outputpro_tool_loop_preamble_filter__",
                False,
            )
        )
        self._patch(ToolLoopAgentRunner, "_iter_llm_responses", iter_factory)
        self._patch(ToolLoopAgentRunner, "step", step_factory)
        if not already_stream_guard_installed and bool(
            getattr(
                getattr(ToolLoopAgentRunner, "_iter_llm_responses", None),
                "__outputpro_tool_stream_guard__",
                False,
            )
        ):
            logger.info(
                "[outputpro:runtime_hook] installed ToolLoopAgentRunner."
                "_iter_llm_responses tool streaming guard"
            )
        if not already_installed and bool(
            getattr(
                getattr(ToolLoopAgentRunner, "step", None),
                "__outputpro_tool_loop_preamble_filter__",
                False,
            )
        ):
            logger.info(
                "[outputpro:runtime_hook] installed ToolLoopAgentRunner.step "
                "tool-call preamble filter"
            )

    async def _filter_chain(
        self,
        chain: MessageChain,
        source: str,
        *,
        session=None,
    ) -> FilterDecision:
        if not chain.chain:
            return FilterDecision(action="drop")

        before_count = len(chain.chain)
        before_plain = collect_visible_text(chain.chain)
        clean_report = sanitize_chain(chain.chain, self.config.clean)
        replace_changes = replace_in_chain(chain.chain, self.config.replace)
        plain = collect_visible_text(chain.chain)

        if clean_report.has_removed():
            logger.debug(
                f"[outputpro:{source}] cleaned outbound chain {before_count}->{len(chain.chain)}: "
                f"{before_plain!r} -> {plain!r}"
            )

        if replace_changes:
            logger.debug(
                f"[outputpro:{source}] replaced outbound text: "
                f"{before_plain!r} -> {plain!r}"
            )

        error_word = self._match_error_keyword(plain)
        if error_word:
            await self._forward_error_if_needed(
                plain,
                session=session,
                source=source,
                keyword=error_word,
            )
            custom_msg = (self.config.error.custom_msg or "").strip()
            if custom_msg:
                chain.chain = MessageChain().message(custom_msg).chain
                logger.warning(
                    f"[outputpro:{source}] intercepted outbound error text by keyword: "
                    f"{error_word}; replaced with custom_msg"
                )
                return FilterDecision(action="replaced", matched_word=error_word)

            logger.warning(
                f"[outputpro:{source}] intercepted outbound error text by keyword: "
                f"{error_word}; dropped directly"
            )
            chain.chain.clear()
            return FilterDecision(action="drop", matched_word=error_word)

        blocked_word = self._match_block_word(plain)
        if blocked_word:
            logger.info(
                f"[outputpro:{source}] blocked outbound text by word: {blocked_word}"
            )
            chain.chain.clear()
            return FilterDecision(action="drop", matched_word=blocked_word)

        return FilterDecision(action="drop" if not chain.chain else "pass")

    async def _forward_error_if_needed(
        self,
        plain: str,
        *,
        session,
        source: str,
        keyword: str,
    ) -> None:
        forward_umo = (self.config.error.forward_umo or "").strip()
        if not forward_umo or not plain:
            return

        context = getattr(self.config, "context", None)
        if context is None:
            return

        chain = MessageChain([Plain(plain)])
        if forward_umo == "admin":
            admin_ids = [
                str(admin_id).strip()
                for admin_id in getattr(self.config, "admins_id", [])
                if str(admin_id).strip()
            ]
            if not admin_ids:
                logger.warning(
                    f"[outputpro:{source}] hit runtime error keyword {keyword}, "
                    "but no admins are configured for forwarding"
                )
                return
            if session is None:
                logger.warning(
                    f"[outputpro:{source}] hit runtime error keyword {keyword}, "
                    "but session context is missing so admin forwarding is skipped"
                )
                return

            failed: list[str] = []
            for admin_id in admin_ids:
                try:
                    admin_session = copy.copy(session)
                    admin_session.session_id = admin_id
                    admin_session.message_type = MessageType.FRIEND_MESSAGE
                    await context.send_message(admin_session, chain)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed.append(admin_id)
                    logger.warning(
                        f"[outputpro:{source}] forward runtime error to admin "
                        f"{admin_id} failed: {exc}"
                    )

            if failed:
                logger.warning(
                    f"[outputpro:{source}] runtime error keyword {keyword} forwarding "
                    f"failed for admins: {','.join(failed)}"
                )
            return

        try:
            await context.send_message(forward_umo, chain)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                f"[outputpro:{source}] forward runtime error to {forward_umo} "
                f"failed: {exc}"
            )

    def _match_block_word(self, plain: str) -> str | None:
        if not plain:
            return None

        for word in self.config.block.block_words:
            if word and word in plain:
                return word
        return None

    def _match_error_keyword(self, plain: str) -> str | None:
        if not plain:
            return None

        for word in self.config.error.keywords:
            if word and word in plain:
                return word
        return None
