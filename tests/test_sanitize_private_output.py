from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


class BaseMessageComponent:
    pass


class Plain(BaseMessageComponent):
    def __init__(self, text: str = ""):
        self.text = text


class Unknown(BaseMessageComponent):
    def __init__(self, text: str = ""):
        self.text = text


class Node(BaseMessageComponent):
    def __init__(self, content=None):
        self.content = content or []


class Nodes(BaseMessageComponent):
    def __init__(self, nodes=None):
        self.nodes = nodes or []


class Reply(BaseMessageComponent):
    def __init__(self, text: str = "", message_str: str = "", chain=None):
        self.text = text
        self.message_str = message_str
        self.chain = chain or []


class TextComponent(BaseMessageComponent):
    def __init__(self, title: str = "", content: str = "", text: str = ""):
        self.title = title
        self.content = content
        self.text = text


class CleanCfg:
    text_threshold = 1000
    bracket = True
    parenthesis = True
    emotion_tag = True
    emoji = True
    lead = ["print", ":", "："]
    tail = ["。", ".", "_"]
    punctuation = "[#%~]"


def install_import_stubs() -> None:
    emoji_module = types.SimpleNamespace(
        EMOJI_DATA={"☃": True, "💬": True},
        replace_emoji=lambda text, replace="": text.replace("☃", replace).replace("💬", replace),
    )

    components_module = types.ModuleType("astrbot.core.message.components")
    components_module.BaseMessageComponent = BaseMessageComponent
    components_module.Location = TextComponent
    components_module.Music = TextComponent
    components_module.Node = Node
    components_module.Nodes = Nodes
    components_module.Plain = Plain
    components_module.Record = TextComponent
    components_module.Reply = Reply
    components_module.Share = TextComponent
    components_module.Unknown = Unknown

    config_module = types.ModuleType("output_patch_under_test.config")
    config_module.CleanConfig = object
    config_module.ReplaceConfig = object

    modules = {
        "emoji": emoji_module,
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.message": types.ModuleType("astrbot.core.message"),
        "astrbot.core.message.components": components_module,
        "output_patch_under_test": types.ModuleType("output_patch_under_test"),
        "output_patch_under_test.config": config_module,
    }
    sys.modules.update(modules)


def load_sanitize():
    install_import_stubs()
    path = Path(__file__).resolve().parents[1] / "core" / "sanitize.py"
    spec = importlib.util.spec_from_file_location(
        "output_patch_under_test.sanitize",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_removes_unmatched_think_block():
    sanitize = load_sanitize()

    cleaned, report = sanitize.clean_text(
        "&&think\n用户问了啥\n最后答案\n应该短一点",
        CleanCfg(),
    )

    assert cleaned == ""
    assert "unmatched_think_block" in report.removed


def test_removes_xml_think_block_but_keeps_visible_text():
    sanitize = load_sanitize()

    cleaned, report = sanitize.clean_text(
        "前面 <think>秘密推理</think> 后面可见",
        CleanCfg(),
    )

    assert cleaned == "前面 后面可见"
    assert "think_xml" in report.removed


def test_removes_private_state_blocks():
    sanitize = load_sanitize()

    cleaned, report = sanitize.clean_text(
        "\u200b咋啦吗 ☃ [Current mood: cozy] 💬 10:00 睡醒 "
        "(Wear: 衣服\n多行内容。) (Current status: 休息) 结尾",
        CleanCfg(),
    )

    assert "[Current mood:" not in cleaned
    assert "(Wear:" not in cleaned
    assert "(Current status:" not in cleaned
    assert "咋啦吗" in cleaned
    assert "10:00 睡醒" in cleaned
    assert "结尾" in cleaned
    assert {"current_mood", "wear_state", "current_status"} <= set(report.removed)


def test_removes_yaml_private_state_line_and_keeps_visible_text():
    sanitize = load_sanitize()

    cleaned, report = sanitize.clean_text(
        "my_mood: tired\n起个鬼 才四点多 别吵我睡觉",
        CleanCfg(),
        emotion_tag=False,
    )

    assert cleaned == "起个鬼 才四点多 别吵我睡觉"
    assert "yaml_private_state" in report.removed


def test_removes_literal_newline_private_state_line_and_keeps_visible_text():
    sanitize = load_sanitize()

    cleaned, report = sanitize.clean_text(
        r"my_mood: tired\n起个鬼 才四点多 别吵我睡觉",
        CleanCfg(),
        emotion_tag=False,
    )

    assert cleaned == "起个鬼 才四点多 别吵我睡觉"
    assert "yaml_private_state" in report.removed


def test_removes_single_warner_status_line():
    sanitize = load_sanitize()

    cleaned, report = sanitize.clean_text(
        "Warner: 准备回复 87袜子",
        CleanCfg(),
        emotion_tag=False,
    )

    assert cleaned == ""
    assert "warner_status" in report.removed


def test_drops_provider_debug_repr():
    sanitize = load_sanitize()

    cleaned, report = sanitize.clean_text(
        "OpenAI completion has no usable output: "
        "ChatCompletion(id='x', choices=[], usage=CompletionUsage())",
        CleanCfg(),
    )

    assert cleaned == ""
    assert "chat_completion_repr" in report.removed


def test_keeps_normal_tool_discussion():
    sanitize = load_sanitize()

    cleaned, report = sanitize.clean_text(
        "后台设置里看下是不是把群聊的工具调用权限给关了，或者群聊没把 tools 传过去",
        CleanCfg(),
    )

    assert cleaned == "后台设置里看下是不是把群聊的工具调用权限给关了，或者群聊没把 tools 传过去"
    assert not report.has_removed()


def test_drops_json_tool_call_debug_blob():
    sanitize = load_sanitize()

    cleaned, report = sanitize.clean_text(
        '{"tool_calls": [{"function": {"name": "grok_web_search"}}]}',
        CleanCfg(),
    )

    assert cleaned == ""
    assert "tool_calls_json" in report.removed


def test_sanitize_chain_prunes_empty_private_output_component():
    sanitize = load_sanitize()
    chain = [
        Plain("ChatCompletion(id='x', choices=[], usage=CompletionUsage())"),
        Plain("正常回复"),
    ]

    report = sanitize.sanitize_chain(chain, CleanCfg())

    assert [component.text for component in chain] == ["正常回复"]
    assert "chat_completion_repr" in report.removed
