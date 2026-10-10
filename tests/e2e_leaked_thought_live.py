"""手动端到端测试: 在 AstrBot 容器内用真实 astrbot 包驱动插件 hook.

用法(容器内): python tests/e2e_leaked_thought_live.py <plugin_parent_dir> <config.json> <samples.json>
"""
from __future__ import annotations

import asyncio, importlib, json, sys, types

parent, cfg_path, samples_path = sys.argv[1:4]
sys.path.insert(0, parent)
mod = importlib.import_module("astrbot_plugin_output_patch.main")
from astrbot.api.provider import LLMResponse
from astrbot.core.message.components import Plain, Image
from astrbot.core.message.message_event_result import MessageChain, MessageEventResult, ResultContentType

cfg = json.load(open(cfg_path, encoding="utf-8-sig"))
ctx = types.SimpleNamespace(get_config=lambda *a, **k: {}, get_all_stars=lambda: [],
                            get_registered_star=lambda *a, **k: None)
plugin = mod.OutputPlugin.__new__(mod.OutputPlugin)
try:
    mod.OutputPlugin.__init__(plugin, ctx, cfg)
except Exception as e:  # 构造依赖较多时退化为只替换必要字段
    print("ctor fallback:", type(e).__name__, e)
    plugin.cfg = mod.PluginConfig(cfg, ctx)
    plugin.pipeline = types.SimpleNamespace()
plugin._get_tts_router_instance = lambda event: None
ran = []
async def fake_run(octx):
    ran.append([c.text for c in octx.chain if isinstance(c, Plain)])
plugin.pipeline.run = fake_run

class Ev:
    def __init__(self, result=None):
        self._r = result; self._extra = {}
        self.message_obj = types.SimpleNamespace(timestamp=0, message_id="1")
    def get_result(self): return self._r
    def set_extra(self, k, v): self._extra[k] = v
    def get_extra(self, k=None, default=None): return self._extra.get(k, default) if k else self._extra
    def get_group_id(self): return "g1"
    def get_sender_id(self): return "u1"
    def get_self_id(self): return "bot"
    def get_platform_name(self): return "aiocqhttp"
    def get_message_type(self): return None
    unified_msg_origin = "aiocqhttp:GroupMessage:379052"

def last_line(t):
    return [l.strip() for l in t.splitlines() if l.strip()][-1]

S = json.load(open(samples_path, encoding="utf-8-sig"))
fails = []
def check(name, got, want):
    if got != want:
        fails.append(name); print("FAIL", name, repr(got)[:120], "!=", repr(want)[:120])

async def llm(text, split=False, with_chain=True):
    r = LLMResponse(role="assistant", completion_text=text)
    if with_chain:
        parts = [text[: len(text)//2], text[len(text)//2:]] if split else [text]
        r.result_chain = MessageChain(chain=[Plain(p) for p in parts])
    await plugin.on_llm_response(Ev(), r)
    vis = "".join(c.text for c in r.result_chain.chain if isinstance(c, Plain)) if with_chain else None
    return r, vis

async def deco(text, split=False, extra=None):
    parts = [text[: len(text)//2], text[len(text)//2:]] if split else [text]
    chain = [Plain(p) for p in parts] + (extra or [])
    res = MessageEventResult(chain=chain); res.set_result_content_type(ResultContentType.LLM_RESULT)
    ran.clear(); await plugin.on_decorating_result(Ev(res))
    return "".join(c.text for c in res.chain if isinstance(c, Plain)), res

async def main():
    n = 0
    for i, t in enumerate(S["leaks"]):
        want = last_line(t)
        for split in (False, True):
            r, vis = await llm(t, split); n += 1
            check(f"llm leak{i} split={split}", vis.strip(), want)
            check(f"llm leak{i} ct", r.completion_text.strip(), want)
            got, _ = await deco(t, split); n += 1
            check(f"deco leak{i} split={split}", got.strip(), want)
        r, _ = await llm(t, with_chain=False); n += 1
        check(f"llm leak{i} ct-only", r.completion_text.strip(), want)
    for i, t in enumerate(S["normal"]):
        got, _ = await deco(t, True); n += 1
        check(f"deco normal{i}", got, t)
        before = mod.strip_leaked_thought(t); n += 1
        check(f"helper normal{i}", before, t)
    edge = {
        "我thought你会来的": "我thought你会来的",
        "thought": "thought",
        "thought\n": "thought\n",
        "Thoughtful reply\n第二行": "Thoughtful reply\n第二行",
        "thoughts\nabc\n好": "thoughts\nabc\n好",
        "  THOUGHT  \r\nthinking...\r\n\r\n最终回复  \n\n": "最终回复",
        "thought\nonly one": "only one",
        "": "",
    }
    for k, v in edge.items():
        n += 1; check(f"edge {k!r}", mod.strip_leaked_thought(k), v)
    check("non-str", mod.strip_leaked_thought(None), None)
    # 带图片的混合链: 图片保留
    img = Image.fromURL("https://example.com/a.png")
    got, res = await deco(S["leaks"][0], True, [img]); n += 1
    check("mixed keeps image", any(isinstance(c, Image) for c in res.chain), True)
    check("mixed text", got.strip(), last_line(S["leaks"][0]))
    print(f"cases={n} fails={len(fails)} pipeline_runs_ok={bool(ran)}")
    sys.exit(1 if fails else 0)

asyncio.run(main())
