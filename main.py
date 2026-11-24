"""给角色一本「原话账本」。

AstrBot 本体会把每个会话的完整消息存进 `data_v4.db` 的 `conversations` 表，但只把
「查群聊历史」做成了工具，私聊这边她翻不到。于是她只能靠记忆摘要——摘要记得住事儿，
记不住原话：主人问「我上次到底怎么说的」「那句我爱你是什么时候说的」，她就只能现编。

这个插件给她两个入口：
- recall_messages：按关键词在最近的对话原文里搜，返回「我说了什么、你回了什么」
- /翻记录：同一条能力，给主人手打用

只读：直接以 `mode=ro` 打开数据库，不写、不改、不删。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

DEFAULT_DB = ""  # 留空则按插件位置反推 <AstrBot>/data/data_v4.db
ROLE_CN = {"user": "你", "assistant": "我", "tool": "工具", "system": "系统"}


# ── 纯函数（可离线自测） ──────────────────────────────────────────────


def message_text(msg: dict) -> str:
    """从一条消息里抠出纯文字；图片/语音等非文字段落跳过。"""
    if not isinstance(msg, dict):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        if part.get("type") not in (None, "text", "plain", "Plain"):
            continue
        text = part.get("text") or part.get("content") or ""
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return " ".join(parts).strip()


def iter_turns(raw: str) -> list[tuple[str, str]]:
    """把 conversations.content 的 JSON 摊成 [(role, text)]，丢掉空与工具轮。"""
    try:
        arr = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(arr, list):
        return []
    turns: list[tuple[str, str]] = []
    for msg in arr:
        role = str((msg or {}).get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        text = message_text(msg)
        if text:
            turns.append((role, text))
    return turns


def _clip(text: str, limit: int) -> str:
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    return body if len(body) <= limit else body[: limit - 1] + "…"


def search_turns(turns: list[tuple[str, str]], keyword: str, limit: int = 5,
                 clip: int = 120) -> list[tuple[str, str]]:
    """按关键词找轮次，返回 [(命中的那句, 紧跟着的下一句)]，新的在后。"""
    needle = str(keyword or "").strip().lower()
    if not turns or not needle:
        return []
    hits: list[tuple[str, str]] = []
    for index, (role, text) in enumerate(turns):
        if needle not in text.lower():
            continue
        follow = turns[index + 1][1] if index + 1 < len(turns) else ""
        hits.append((f"{ROLE_CN.get(role, role)}：{_clip(text, clip)}", _clip(follow, clip)))
    return hits[-max(1, limit):]


def render_hits(hits: list[tuple[str, str]], turns_total: int) -> str:
    if not hits:
        return (
            "最近的对话里没找到这个说法。换个词试试；再早的原文 AstrBot 会裁掉，"
            "那时候只能靠记忆摘要——不过摘要记得住事、记不住原话，这点得跟主人说清楚。"
        )
    lines = [f"在最近 {turns_total} 句对话里翻到 {len(hits)} 处："]
    for i, (line, follow) in enumerate(hits, 1):
        lines.append(f"{i}. {line}")
        if follow:
            lines.append(f"   接的是 → {follow}")
    return "\n".join(lines)


# ── 插件 ─────────────────────────────────────────────────────────────


class DafeiyuRecall(Star):
    """她的原话账本。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config

    # 读取配置
    def _db_path(self) -> Path:
        raw = str(self.conf.get("db_path") or DEFAULT_DB).strip()
        if raw:
            return Path(raw)
        # plugins/<name>/main.py → <AstrBot>/data/plugins/<name> → 上两级就是 <AstrBot>/data
        return Path(__file__).resolve().parents[2] / "data_v4.db"

    def _limit(self, fallback: int = 5) -> int:
        try:
            value = int(self.conf.get("max_quotes") or fallback)
        except (TypeError, ValueError):
            value = fallback
        return max(1, min(12, value))

    def _clip(self) -> int:
        try:
            value = int(self.conf.get("max_chars") or 120)
        except (TypeError, ValueError):
            value = 120
        return max(40, min(300, value))

    def _load(self, scope: str, umo: str) -> tuple[list[tuple[str, str]], int]:
        """从库里读会话原文；scope=当前会话 只读本条会话，否则把主人的话都读进来。"""
        db = self._db_path()
        if not db.exists():
            raise FileNotFoundError(f"找不到数据库 {db}")
        uri = f"file:{db.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            rows = list(conn.execute(
                "SELECT user_id, content FROM conversations ORDER BY updated_at DESC",
            ))
        finally:
            conn.close()

        merged: list[tuple[str, str]] = []
        sessions = 0
        for user_id, content in rows:
            uid = str(user_id or "")
            if not uid:
                continue
            if scope != "全部" and umo and uid != umo:
                continue
            turns = iter_turns(content)
            if not turns:
                continue
            sessions += 1
            merged.extend(turns)
        return merged, sessions

    @filter.llm_tool(name="recall_messages")
    async def recall_messages(self, event: AstrMessageEvent, keyword: str, scope: str = "当前会话") -> str:
        """翻我们聊过的原话。当主人问「我上次怎么说的」「你当时是原话是什么」「那句话是什么时候说的」，
        或者你想确认某个词他到底说没说过、原话长什么样，就用它——别凭记忆摘要现编。

        Args:
            keyword(string): 要找的词或短语，比如「高数」「泰勒」「我爱你」「作业」
            scope(string): 「当前会话」只翻这一条聊天；写「全部」会把 QQ/微信/桌宠所有会话一起翻
        """
        needle = str(keyword or "").strip()
        if not needle:
            return "还没告诉我找什么词呢。"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        where = "全部会话" if str(scope or "").strip() == "全部" else "当前会话"
        try:
            turns, sessions = self._load(str(scope or "").strip(), umo)
        except FileNotFoundError as exc:
            return f"翻不到记录：{exc}。"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[recall] 读库失败: %s", exc)
            return "账本这会儿打不开，稍后再试。"

        hits = search_turns(turns, needle, limit=self._limit(), clip=self._clip())
        logger.info("[recall] 查「%s」%s：%s 个会话 %s 句，命中 %s 处",
                    needle[:20], where, sessions, len(turns), len(hits))
        return f"（{where}，共 {len(turns)} 句）\n" + render_hits(hits, len(turns))

    @filter.command("翻记录")
    async def recall_command(self, event: AstrMessageEvent, keyword: str = "") -> None:
        """手打 /翻记录 关键词，直接翻原文。"""
        needle = str(keyword or "").strip()
        if not needle:
            yield event.plain_result("用法：/翻记录 关键词，比如 /翻记录 高数")
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            turns, _ = self._load("当前会话", umo)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[recall] 命令读库失败: %s", exc)
            yield event.plain_result("账本这会儿打不开，稍后再试。")
            return
        hits = search_turns(turns, needle, limit=self._limit(), clip=self._clip())
        yield event.plain_result(render_hits(hits, len(turns)))
