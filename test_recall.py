"""离线自测：不装 AstrBot 也能跑。
用 F:\\dafeiyu\\uv\\tools\\astrbot\\Scripts\\python.exe -X utf8 test_recall.py
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ── 把 astrbot 的几个 import 换成空壳 ────────────────────────────────


def _stub() -> None:
    astrbot = types.ModuleType("astrbot")
    astrbot.logger = types.SimpleNamespace(info=lambda *a, **k: None,
                                           warning=lambda *a, **k: None)
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    star_mod = types.ModuleType("astrbot.api.star")
    cfg_mod = types.ModuleType("astrbot.core.config.astrbot_config")

    class _FakeFilter:
        @staticmethod
        def llm_tool(**_kwargs):
            def deco(fn):
                return fn
            return deco

        @staticmethod
        def command(*_args, **_kwargs):
            def deco(fn):
                return fn
            return deco

    event_mod.filter = _FakeFilter()
    event_mod.AstrMessageEvent = type("AstrMessageEvent", (), {})
    star_mod.Context = type("Context", (), {})
    star_mod.Star = type("Star", (), {"__init__": lambda self, ctx=None: None})
    cfg_mod.AstrBotConfig = dict

    for name, mod in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.star": star_mod,
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.config": types.ModuleType("astrbot.core.config"),
        "astrbot.core.config.astrbot_config": cfg_mod,
    }.items():
        sys.modules[name] = mod


_stub()
spec = importlib.util.spec_from_file_location("recall_main", ROOT / "main.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[union-attr]

failures: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")


# 1) 文本抽取：文字段落拼起来，非文字跳过
msg = {"role": "user", "content": [
    {"type": "text", "text": "他说"},
    {"type": "image", "url": "x"},
    {"type": "text", "text": "这样"},
]}
check("message_text 拼文字", mod.message_text(msg), "他说 这样")
check("message_text 字符串内容", mod.message_text({"content": " 原样 "}), "原样")
check("message_text 空", mod.message_text({"content": None}), "")

# 2) 摊平 JSON，丢掉工具轮与空文本
raw = __import__("json").dumps([
    {"role": "user", "content": [{"type": "text", "text": "高数好难"}]},
    {"role": "tool", "content": "忽略我"},
    {"role": "assistant", "content": [{"type": "text", "text": "先做泰勒展开"}]},
    {"role": "assistant", "content": [{"type": "text", "text": ""}]},
], ensure_ascii=False)
turns = mod.iter_turns(raw)
check("iter_turns 数量", len(turns), 2)
check("iter_turns 顺序", turns[0], ("user", "高数好难"))
check("坏 JSON 不抛", mod.iter_turns("{oops"), [])

# 3) 搜索带上下文
hits = mod.search_turns(turns, "高数", limit=3)
check("命中数", len(hits), 1)
check("命中行", hits[0][0], "你：高数好难")
check("接的话", hits[0][1], "先做泰勒展开")

# 4) 大小写不敏感 + 关键词为空
check("大小写", len(mod.search_turns([("user", "Hello 世界")], "hello")), 1)
check("空关键词", mod.search_turns(turns, "   "), [])

# 5) limit 只留最后几条
many = [("user", f"第{i}次问高数") for i in range(10)]
check("limit", len(mod.search_turns(many, "高数", limit=4)), 4)
check("limit 留最后", mod.search_turns(many, "高数", limit=1)[0][0], "你：第9次问高数")

# 6) 截断
long_text = "很" * 300
row = mod.search_turns([("user", long_text)], "很", clip=50)[0][0]
check("截断长度", len(row), len("你：") + 50)
check("截断带省略号", row.endswith("…"), True)

# 7) 渲染
check("空结果文案", mod.render_hits([], 3).startswith("最近的对话里没找到"), True)
rendered = mod.render_hits([("你：高数好难", "先做泰勒展开")], 12)
check("渲染含条数", "翻到 1 处" in rendered, True)
check("渲染含接续", "先做泰勒展开" in rendered, True)

# 8) 真库能不能只读打开
db = Path(__file__).resolve().parents[2] / "data_v4.db"
if db.exists():
    plugin = mod.DafeiyuRecall(None, {"db_path": str(db), "max_quotes": 3, "max_chars": 60})
    loaded, sessions = plugin._load("当前会话", "demo_platform:FriendMessage:10001")
    check("真库能读", isinstance(loaded, list) and sessions >= 1, True)
    check("真库有内容", len(loaded) > 10, True)
else:
    print("跳过真库检查：库不在")

if failures:
    print("FAIL")
    for item in failures:
        print(" -", item)
    raise SystemExit(1)
print("ALL OK")
