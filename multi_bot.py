"""Run all Telegram bots in parallel, each backed by a Managed Agent.

Each bot:
  - /start  — greet + describe its agent
  - /help   — what this agent does
  - any text message — creates a fresh Managed Agents session, streams reply
                       back to Telegram, prefixed with agent name + emoji.

Orchestrator bot adds:
  - /team   — list all agents with their @usernames
  - on text — first prints which agent it would delegate to (parsed from
              the orchestrator's JSON response), then forwards the JSON.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from anthropic import AsyncAnthropic
from telegram import Bot, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env", override=True)

BETA_HEADER = "managed-agents-2026-04-01"

GROUP_CHAT_ID = (os.environ.get("GROUP_CHAT_ID") or "").strip() or None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("multi_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


# ------------------------------------------------------------------ metadata

@dataclass(frozen=True)
class AgentMeta:
    name: str           # internal id (matches agents_config.json)
    emoji: str
    title: str          # short label
    display: str        # full Russian name shown in group replies
    token_env: str      # env var holding the bot token
    short: str          # one-line description for /start
    help: str           # multi-line for /help


AGENTS: list[AgentMeta] = [
    AgentMeta("orchestrator", "🎯", "Orchestrator", "Андрей Оркестратор", "TELEGRAM_ORCHESTRATOR_TOKEN",
              "Координатор команды. Анализирует задачу и решает кому её делегировать.",
              "Я — координатор. Опиши задачу одним сообщением — отвечу JSON-разбором: "
              "какому агенту это лучше отдать, с каким приоритетом и контекстом.\n"
              "/team — список всех агентов с их @username."),
    AgentMeta("researcher", "🔍", "Researcher", "Милена Маркетолог", "TELEGRAM_RESEARCHER_TOKEN",
              "Аналитик и сборщик данных по Telegram, СНГ-рынку, инфопродуктам.",
              "Работаю по схеме: Факты → Инсайты → Рекомендации.\n"
              "Пиши задачу на исследование (конкуренты, ниша, тренд) — отвечу со ссылками."),
    AgentMeta("strategist", "📐", "Strategist", "Александр Стратег", "TELEGRAM_STRATEGIST_TOKEN",
              "Планирование на горизонты неделя/месяц/квартал.",
              "Дам 2–3 варианта стратегии с плюсами/минусами и рекомендую один.\n"
              "Формат: Цель → Шаги → Метрики → Риски."),
    AgentMeta("content_writer", "✍️", "Content Writer", "Алина Копирайтер", "TELEGRAM_WRITER_TOKEN",
              "Контент для Telegram-каналов и инфопродуктов.",
              "Пишу как живой человек, без воды. Дам 2 варианта: безопасный и экспериментальный.\n"
              "Скажи: тема, формат (мнение/история/список), аудитория."),
    AgentMeta("dev_agent", "💻", "Dev", "Михаил Кодер", "TELEGRAM_DEV_TOKEN",
              "Senior-разработчик: TS/JS, Python, Telegram Bot API, Mini Apps.",
              "Опиши задачу. Получишь: код → структура файлов → как запустить → известные ограничения."),
    AgentMeta("analyst", "📊", "Analyst", "Николай Аналитик", "TELEGRAM_ANALYST_TOKEN",
              "Метрики Telegram, рекламная аналитика, воронки.",
              "Кидай цифры — объясню что они значат, сравню с бенчмарками, дам одно конкретное действие."),
    AgentMeta("sales_agent", "💰", "Sales", "Виктор Продажи", "TELEGRAM_SALES_TOKEN",
              "Продажи инфопродуктов и работа с клиентами.",
              "Скрипты продаж, отработка возражений, выстраивание LTV.\n"
              "Опиши клиента/возражение — отвечу по схеме Acknowledge → Reframe → Prove → Next step."),
    AgentMeta("critic", "🛡", "Critic", "Критик", "TELEGRAM_CRITIC_TOKEN",
              "Контроль качества контента и кода.",
              "Кидай текст или код — найду проблемы по чек-листу.\n"
              "Вердикт: APPROVED / NEEDS REVISION."),
]
AGENTS_BY_NAME: dict[str, AgentMeta] = {a.name: a for a in AGENTS}


# ------------------------------------------------------------------ config

def load_config() -> dict:
    cfg = json.loads((HERE / "agents_config.json").read_text())
    if "agents" not in cfg:
        raise RuntimeError("agents_config.json is missing 'agents' map")
    return cfg


# ---------------------------------------------------------- shared registry

# Filled at startup as each bot fetches its own @username
TEAM_REGISTRY: dict[str, str] = {}  # agent_name -> "@botusername"
BOTS: dict[str, Bot] = {}           # agent_name -> Bot instance (for cross-bot sends)


# -------------------------------------------------------- Anthropic client

def make_anthropic() -> AsyncAnthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing")
    return AsyncAnthropic(
        api_key=api_key,
        default_headers={"anthropic-beta": BETA_HEADER},
        timeout=120.0,
        max_retries=2,
    )


ANTHROPIC: AsyncAnthropic | None = None
ENVIRONMENT_ID: str | None = None
AGENT_IDS: dict[str, str] = {}  # agent_name -> agent_id, populated in main()
MEMORY_STORES: dict[str, str] = {}  # agent_name -> memstore_id

# (agent_name, chat_id) -> {"mem_id": str, "ts": float}
LAST_TASK: dict[tuple[str, int], dict] = {}
# chat_id -> {"agent_name": str, "mem_id": str, "ts": float}  (group only)
LAST_TASK_GROUP: dict[int, dict] = {}
FEEDBACK_TTL_SEC = 30 * 60   # feedback only applies if last task < 30 min ago


# ------------------------------------------------------- streaming helper

RUN_SESSION_TIMEOUT_S = 60     # whole-operation timeout per user message
TIMEOUT_FALLBACK_TEXT = "⏱ Агент не отвечает, попробуй ещё раз"

# Agents whose answers get an automatic Critic review pass.
AGENTS_REVIEWED_BY_CRITIC = {"content_writer", "dev_agent", "analyst"}
MAX_REVIEW_ITERS = 3
_VERDICT_RE = re.compile(
    r"(?:ВЕРДИКТ|VERDICT)\s*[:\-—]\s*"
    r"(ПРИНЯТО|ДОРАБОТАТЬ|APPROVED|NEEDS[\s_]+REVISION)",
    re.IGNORECASE,
)


def _parse_critic_verdict(text: str) -> str:
    m = _VERDICT_RE.search(text or "")
    if not m:
        return "UNKNOWN"
    v = m.group(1).upper().replace(" ", "_")
    return "APPROVED" if v in ("ПРИНЯТО", "APPROVED") else "NEEDS_REVISION"


async def run_with_critic_review(
    agent_name: str, agent_id: str, original_task: str,
    *, status_cb=None, max_iters: int = MAX_REVIEW_ITERS,
) -> tuple[str, list[dict]]:
    """Run agent → Critic → maybe redo loop.

    Returns (final_text, history) where history records each iteration:
      {iter, verdict, critic_note}.
    Critic itself does not post anywhere — only the final answer reaches
    the user.
    """
    critic_id = AGENT_IDS.get("critic")
    history: list[dict] = []
    last_answer = ""
    last_critic_note = ""

    for i in range(1, max_iters + 1):
        if i == 1:
            cur_task = original_task
        else:
            cur_task = (
                f"{original_task}\n\n"
                f"--- Замечания Critic к предыдущей версии ---\n"
                f"{last_critic_note}\n"
                f"--- Конец замечаний ---\n"
                f"Перепиши ответ с учётом этих замечаний. Не игнорируй ни одно."
            )
        if status_cb:
            try:
                await status_cb(f"итерация {i}/{max_iters}: работаю…")
            except Exception:
                pass

        try:
            last_answer = await run_session(
                agent_id, cur_task, on_chunk=None, agent_name=agent_name)
        except Exception as e:
            log.exception("[%s] review iter %d agent run failed", agent_name, i)
            history.append({"iter": i, "verdict": "AGENT_FAIL",
                            "critic_note": str(e)})
            break

        # Last iteration — accept whatever we have
        if not critic_id or i == max_iters:
            history.append({"iter": i,
                            "verdict": "NO_CRITIC" if not critic_id else "FINAL",
                            "critic_note": ""})
            break

        if status_cb:
            try:
                await status_cb(f"итерация {i}/{max_iters}: проверяет Critic…")
            except Exception:
                pass

        critic_prompt = (
            f"Исходная задача:\n{original_task}\n\n"
            f"Ответ агента:\n{last_answer}\n\n"
            f"Проверь по своим критериям. В конце обязательно строка "
            f"\"ВЕРДИКТ: ПРИНЯТО\" или \"ВЕРДИКТ: ДОРАБОТАТЬ\". "
            f"Если ДОРАБОТАТЬ — сформулируй конкретные пункты что исправить."
        )
        try:
            critic_text = await run_session(
                critic_id, critic_prompt, on_chunk=None,
                agent_name="critic", attach_memory=False)
        except Exception as e:
            log.exception("[critic] iter %d failed", i)
            history.append({"iter": i, "verdict": "CRITIC_FAIL",
                            "critic_note": str(e)})
            break

        verdict = _parse_critic_verdict(critic_text)
        history.append({"iter": i, "verdict": verdict,
                        "critic_note": critic_text})
        log.info("[review %s] iter %d → %s", agent_name, i, verdict)

        if verdict == "APPROVED":
            break
        last_critic_note = critic_text.strip()[:1500]

    return (last_answer or "(пустой ответ)"), history


def _format_review_footer(history: list[dict]) -> str:
    if not history:
        return ""
    last = history[-1]
    n = len(history)
    if last["verdict"] == "APPROVED" and n == 1:
        return ""
    if last["verdict"] == "APPROVED":
        return f"\n\n↻ принято Critic с {n}-й итерации"
    if last["verdict"] == "NO_CRITIC":
        return ""
    if last["verdict"] in ("CRITIC_FAIL", "AGENT_FAIL"):
        return f"\n\n↻ {n} итераций (Critic недоступен)"
    return f"\n\n↻ Critic не одобрил после {n} итераций — отдаю последнюю версию"


async def _run_session_impl(agent_id: str, user_text: str, on_chunk,
                            *, agent_name: Optional[str] = None,
                            attach_memory: bool = True) -> str:
    assert ANTHROPIC is not None and ENVIRONMENT_ID is not None
    label = agent_name or agent_id

    session_kwargs: dict = {
        "agent": agent_id,
        "environment_id": ENVIRONMENT_ID,
        "title": "telegram",
    }
    if attach_memory and agent_name and MEMORY_STORES.get(agent_name):
        session_kwargs["resources"] = [{
            "type": "memory_store",
            "memory_store_id": MEMORY_STORES[agent_name],
            "access": "read_write",
            "instructions": (
                "Перед началом работы прочитай файлы в /tasks/ (последние 5–10) "
                "и в /profiles/ (профили проектов и клиентов). "
                "Используй контекст: не переспрашивай то, что уже знаешь. "
                "Писать в память самостоятельно НЕ нужно — host сделает запись после."
            ),
        }]

    log.info("[%s] creating MA session for agent=%s (memory=%s)",
             label, agent_id, bool(session_kwargs.get("resources")))
    session = await ANTHROPIC.beta.sessions.create(**session_kwargs)
    log.info("[%s] session created: %s", label, session.id)

    final_text_parts: list[str] = []
    stream_cm = await ANTHROPIC.beta.sessions.events.stream(session.id)
    async with stream_cm as stream:
        log.info("[%s] sending user.message (len=%d)", label, len(user_text))
        await ANTHROPIC.beta.sessions.events.send(
            session.id,
            events=[{
                "type": "user.message",
                "content": [{"type": "text", "text": user_text}],
            }],
        )
        log.info("[%s] awaiting agent reply…", label)

        async for event in stream:
            etype = getattr(event, "type", None)
            if etype == "agent.message":
                for block in getattr(event, "content", []) or []:
                    text = getattr(block, "text", None)
                    if text:
                        final_text_parts.append(text)
                        if on_chunk is not None:
                            await on_chunk("".join(final_text_parts))
            elif etype == "session.status_idle":
                break

    full = "".join(final_text_parts).strip() or "(пустой ответ)"
    log.info("[%s] reply ready: %d chars", label, len(full))
    return full


async def run_session(agent_id: str, user_text: str, on_chunk,
                      *, agent_name: Optional[str] = None,
                      attach_memory: bool = True) -> str:
    """Create a Managed Agents session per user message and stream the reply.

    Wrapped in a 60 s wall-clock timeout — if the agent hangs we surface a
    user-visible message instead of leaving the chat silent forever.
    """
    label = agent_name or agent_id
    try:
        return await asyncio.wait_for(
            _run_session_impl(agent_id, user_text, on_chunk,
                              agent_name=agent_name,
                              attach_memory=attach_memory),
            timeout=RUN_SESSION_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        log.warning("[%s] run_session timed out after %ds",
                    label, RUN_SESSION_TIMEOUT_S)
        return TIMEOUT_FALLBACK_TEXT
    except Exception as e:
        log.exception("[%s] run_session failed: %s", label, e)
        raise


# ----------------------------------------------------------- greetings

GREETINGS: dict[str, str] = {
    "orchestrator":   "🎯 Андрей Оркестратор здесь.\nКоординирую команду — кидай любую задачу, я разберусь кому передать.",
    "researcher":     "🔍 Милена на связи.\nАнализирую конкурентов, исследую ниши, ищу данные.",
    "strategist":     "📐 Александр здесь.\nСтрою контент-планы, воронки и стратегии роста.",
    "content_writer": "✍️ Алина на связи.\nПишу посты, продающие тексты и скрипты для Telegram.",
    "dev_agent":      "💻 Михаил здесь.\nРазрабатываю мини-аппы, ботов и интеграции.",
    "analyst":        "📊 Николай на связи.\nАнализирую метрики, ROI и данные бизнеса.",
    "sales_agent":    "💰 Виктор здесь.\nСтрою воронки продаж и работаю с возражениями.",
}
GREETING_FINAL = "✅ Команда в сборе. Кидай задачу — начинаем."

GREETING_ORDER = ["researcher", "strategist", "content_writer",
                  "dev_agent", "analyst", "sales_agent"]

# Single regex to detect ANY trigger word, case-insensitive, word-boundary aware.
import re as _re
_GREET_RE = _re.compile(
    r"(?<![\w@])(?:привет|hello|hi|hey|салют|здарова|хай|ау|команда)(?![\w])",
    _re.IGNORECASE,
)


def _is_greeting(text: str) -> bool:
    return bool(_GREET_RE.search(text or ""))


async def _run_group_greeting(chat_id: int) -> None:
    """Sequential team greeting — orchestrator first, specialists in order, then a final line."""
    orch = BOTS.get("orchestrator")
    if orch is None:
        return
    try:
        await orch.send_message(chat_id=chat_id, text=GREETINGS["orchestrator"])
        for name in GREETING_ORDER:
            bot = BOTS.get(name)
            text = GREETINGS.get(name)
            if not bot or not text:
                continue
            await asyncio.sleep(1.0)
            try:
                await bot.send_message(chat_id=chat_id, text=text)
            except Exception as e:
                log.warning("greeting send failed for %s: %s", name, e)
        await asyncio.sleep(1.0)
        await orch.send_message(chat_id=chat_id, text=GREETING_FINAL)
    except Exception:
        log.exception("group greeting sequence failed")


# ----------------------------------------------------------- memory helpers

# Feedback triggers are only valid as a STANDALONE short reply — never as a word
# inside a longer task ("топ каналы про X" must NOT count as positive feedback).
_POS_FB_RE = re.compile(
    r"^\s*(?:хорошо|отлично|то\s+что\s+надо|супер|огонь|круто|класс|топ|годно|спасибо|👍|👌|✅|🔥|❤️|💯)"
    r"\s*[!.…,)]*\s*$",
    re.IGNORECASE,
)
# Negative may carry a short note after the trigger ("переделай, слишком длинно").
_NEG_FB_RE = re.compile(
    r"^\s*(?:переделай|переделать|не\s+то|плохо|не\s+подходит|ужасно|мусор|👎|❌)"
    r"(?:\s*[,:!.…—\-]\s*.{0,200}|\s*[!.…,)]*)?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _detect_feedback(text: str) -> Optional[str]:
    s = (text or "").strip()
    if not s or len(s) > 250:
        return None
    if _POS_FB_RE.match(s):
        return "positive"
    if _NEG_FB_RE.match(s):
        return "negative"
    return None


def _safe_proj_path(name: str) -> str:
    s = re.sub(r"[^\w\-]+", "_", (name or "general").strip()).strip("_")
    return (s or "general")[:60]


def _today_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _ts_iso() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


async def _mem_create(agent_name: str, path: str, content: str):
    sid = MEMORY_STORES.get(agent_name)
    if not sid or ANTHROPIC is None:
        return None
    try:
        return await ANTHROPIC.beta.memory_stores.memories.create(
            sid, path=path, content=content[:80_000])
    except Exception as e:
        log.warning("mem_create %s %s failed: %s", agent_name, path, e)
        return None


async def _mem_retrieve(agent_name: str, mem_id: str):
    sid = MEMORY_STORES.get(agent_name)
    if not sid or ANTHROPIC is None:
        return None
    try:
        return await ANTHROPIC.beta.memory_stores.memories.retrieve(
            mem_id, memory_store_id=sid)
    except Exception as e:
        log.warning("mem_retrieve %s failed: %s", mem_id, e)
        return None


async def _mem_update(agent_name: str, mem_id: str, content: str,
                      sha: Optional[str] = None):
    sid = MEMORY_STORES.get(agent_name)
    if not sid or ANTHROPIC is None:
        return None
    kwargs: dict = {"memory_store_id": sid, "content": content[:80_000]}
    if sha:
        kwargs["precondition"] = {"type": "content_sha256",
                                  "content_sha256": sha}
    try:
        return await ANTHROPIC.beta.memory_stores.memories.update(mem_id, **kwargs)
    except Exception as e:
        log.warning("mem_update %s failed: %s", mem_id, e)
        return None


async def _mem_list(agent_name: str, prefix: str) -> list:
    sid = MEMORY_STORES.get(agent_name)
    if not sid or ANTHROPIC is None:
        return []
    try:
        page = await ANTHROPIC.beta.memory_stores.memories.list(
            sid, path_prefix=prefix, depth=10)
        return list(page.data)
    except Exception as e:
        log.warning("mem_list %s %s failed: %s", agent_name, prefix, e)
        return []


async def _mem_delete(agent_name: str, mem_id: str) -> None:
    sid = MEMORY_STORES.get(agent_name)
    if not sid or ANTHROPIC is None:
        return
    try:
        await ANTHROPIC.beta.memory_stores.memories.delete(
            mem_id, memory_store_id=sid)
    except Exception as e:
        log.warning("mem_delete %s failed: %s", mem_id, e)


async def record_task(agent_name: str, chat_id: int, *,
                      task: str, result: str, project: Optional[str] = None) -> None:
    """Save a private/single-task record to the agent's memory store."""
    record = {
        "date": _today_iso(),
        "task": (task or "")[:600],
        "project": project,
        "result": (result or "")[:1800],
        "user_feedback": None,
    }
    fid = uuid.uuid4().hex[:8]
    path = f"/tasks/{_today_iso()}-{fid}.json"
    m = await _mem_create(agent_name, path,
                          json.dumps(record, ensure_ascii=False, indent=2))
    if m:
        LAST_TASK[(agent_name, chat_id)] = {"mem_id": m.id, "ts": time.time()}
        LAST_TASK_GROUP[chat_id] = {"agent_name": agent_name,
                                    "mem_id": m.id, "ts": time.time()}
        # Lazy compression check (do not block caller)
        asyncio.create_task(_maybe_compress(agent_name))


async def record_pipeline_step(agent_name: str, chat_id: int, *,
                               project: str, step_no: int,
                               my_contribution: str,
                               data_received: str,
                               data_passed: str) -> None:
    record = {
        "date": _today_iso(),
        "project": project,
        "step": step_no,
        "my_contribution": (my_contribution or "")[:1800],
        "data_received": (data_received or "")[:1200],
        "data_passed": (data_passed or "")[:1200],
    }
    safe = _safe_proj_path(project)
    path = f"/projects/{safe}/step-{step_no:02d}-{agent_name}.json"
    m = await _mem_create(agent_name, path,
                          json.dumps(record, ensure_ascii=False, indent=2))
    if m:
        LAST_TASK[(agent_name, chat_id)] = {"mem_id": m.id, "ts": time.time()}
        LAST_TASK_GROUP[chat_id] = {"agent_name": agent_name,
                                    "mem_id": m.id, "ts": time.time()}


async def record_project_summary(agent_name: str, project: str,
                                 summary: str) -> None:
    """Drop a per-project profile file into agent's memory after pipeline ends."""
    safe = _safe_proj_path(project)
    path = f"/profiles/{safe}.md"
    content = (f"# Профиль проекта: {project}\n"
               f"Last updated: {_today_iso()}\n\n{summary}")
    # Delete any existing same-named profile, then write fresh
    existing = await _mem_list(agent_name, path)
    for it in existing:
        if getattr(it, "path", None) == path:
            await _mem_delete(agent_name, it.id)
            break
    await _mem_create(agent_name, path, content)


async def apply_feedback(agent_name: str, mem_id: str,
                         feedback: str, note: str) -> None:
    m = await _mem_retrieve(agent_name, mem_id)
    if not m:
        return
    try:
        data = json.loads(getattr(m, "content", "") or "{}")
    except Exception:
        data = {"raw": getattr(m, "content", "")}
    data["user_feedback"] = feedback
    if feedback == "negative":
        data["feedback_note"] = (note or "")[:400]
    new_content = json.dumps(data, ensure_ascii=False, indent=2)
    await _mem_update(agent_name, mem_id, new_content,
                      sha=getattr(m, "content_sha256", None))


async def _maybe_compress(agent_name: str) -> None:
    """Every 10 task records → compress oldest into a profile, delete originals."""
    items = await _mem_list(agent_name, "/tasks/")
    if len(items) < 10:
        return
    oldest = sorted(items, key=lambda x: getattr(x, "path", ""))[:10]
    contents: list[str] = []
    for it in oldest:
        full = await _mem_retrieve(agent_name, it.id)
        if full and getattr(full, "content", None):
            contents.append(f"--- {it.path} ---\n{full.content}")
    prompt = (
        "Сожми эти записи задач в один компактный профиль (plain text без markdown). "
        "Что узнали о клиенте/проектах, какие подходы сработали, повторяющиеся темы, "
        "стиль и предпочтения. Короткие абзацы, без воды.\n\n"
        + "\n\n".join(contents)
    )[:30_000]

    renderer = AGENT_IDS.get("strategist") or AGENT_IDS.get("content_writer")
    if not renderer:
        return
    try:
        # IMPORTANT: don't attach this agent's memory store to the compressor —
        # we're producing the summary ABOUT it, no need to read it back into context.
        summary = await run_session(renderer, prompt, on_chunk=None,
                                    attach_memory=False)
    except Exception as e:
        log.warning("compression failed for %s: %s", agent_name, e)
        return
    summary = _finalize(summary)
    path = f"/profiles/general-{_ts_iso()}.md"
    await _mem_create(agent_name, path, summary)
    for it in oldest:
        await _mem_delete(agent_name, it.id)
    log.info("memory compressed for %s: %d tasks → %s", agent_name, len(oldest), path)


# ----------------------------------------------------------- direct delegation

async def execute_agent(target_name: str, task: str, chat_id: int) -> None:
    """Run a Managed Agents session for `target_name` and post the result
    to `chat_id` via that agent's Telegram bot. No bot-to-bot mention loop —
    this is an in-process function call, the message we post is final and
    addressed directly to the user."""
    target_meta = AGENTS_BY_NAME.get(target_name)
    bot = BOTS.get(target_name)
    target_id = AGENT_IDS.get(target_name)
    if not (target_meta and bot and target_id):
        log.warning("execute_agent: target %s not available", target_name)
        return

    label = f"{target_meta.emoji} {target_meta.display}:"

    try:
        placeholder = await bot.send_message(chat_id=chat_id,
                                             text=f"{label}\nработаю…")
    except Exception as e:
        log.warning("execute_agent: cannot post to chat %s: %s", chat_id, e)
        return

    review_footer = ""
    try:
        if (target_name in AGENTS_REVIEWED_BY_CRITIC
                and AGENT_IDS.get("critic")):
            async def _status(s: str):
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=placeholder.message_id,
                        text=f"{label}\n{s}",
                    )
                except Exception:
                    pass
            final, history = await run_with_critic_review(
                target_name, target_id, task, status_cb=_status)
            review_footer = _format_review_footer(history)
        else:
            final = await run_session(target_id, task, on_chunk=None,
                                      agent_name=target_name)
    except Exception as e:
        log.exception("execute_agent: session failed for %s", target_name)
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=placeholder.message_id,
                text=f"{label}\n⚠️ Ошибка: {type(e).__name__}: {e}",
            )
        except Exception:
            pass
        return

    final = _finalize(final) + review_footer
    await send_long(bot, chat_id, f"{label}\n{final}",
                    first_message=placeholder)
    asyncio.create_task(record_task(target_name, chat_id,
                                    task=task, result=final))


# ----------------------------------------------------------- project pipeline

PROJECTS_DIR = HERE / "projects"
PROJECTS_DIR.mkdir(exist_ok=True)

# chat_id -> pipeline state dict
ACTIVE_PIPELINES: dict[int, dict] = {}

_PROJECT_RE = re.compile(
    r"(?<![\w])(?:новый\s+проект|проект:|запускаем|работаем\s+над)(?![\w])",
    re.IGNORECASE,
)
_STOP_RE = re.compile(r"(?<![\w])стоп\s+проект(?![\w])", re.IGNORECASE)
_STATUS_RE = re.compile(r"(?<![\w])статус\s+проект[ауы]?(?![\w])", re.IGNORECASE)

PIPELINE = [
    (1, "researcher",
     "Проанализируй нишу, конкурентов и целевую аудиторию для этого проекта. "
     "Дай: 5 ключевых конкурентов (имя/аудитория/слабые места), портрет ЦА, "
     "тренды и риски ниши. Будь конкретен."),
    (2, "strategist",
     "Опираясь на анализ Researcher, построй стратегию на месяц вперёд: "
     "цель, шаги, метрики, риски, и контент-план на 4 недели в формате таблицы "
     "Неделя | Тема | Формат | Цель | Метрика."),
    (3, "content_writer",
     "На основе стратегии напиши 3 ПЕРВЫХ поста в Telegram (готовых к публикации). "
     "Для каждого поста дай: тема, формат, текст. Без воды."),
    (4, "analyst",
     "Определи 5–7 ключевых метрик и KPI для отслеживания этого проекта. "
     "По каждой метрике: что измеряем, как считаем, целевое значение, частота снятия."),
    (5, "critic",
     "Проверь все результаты предыдущих агентов. Найди слабые места и неточности. "
     "Сформулируй: ✅ что хорошо, ❌ что доработать, ⚠️ предложения. Финальный вердикт."),
]


def _is_project_start(text: str) -> bool:
    return bool(_PROJECT_RE.search(text or ""))


def _is_stop_pipeline(text: str) -> bool:
    return bool(_STOP_RE.search(text or ""))


def _is_status_pipeline(text: str) -> bool:
    return bool(_STATUS_RE.search(text or ""))


def _extract_project_name(text: str) -> str:
    """Try to derive a short project name from the trigger sentence."""
    t = text.strip()
    m = re.search(r"(?:проект:|новый\s+проект|запускаем|работаем\s+над)\s*(.{2,80})",
                  t, re.IGNORECASE)
    if m:
        candidate = m.group(1).strip(" .,:;!?\n\r\t-")
        candidate = candidate.split("\n", 1)[0]
        if candidate:
            return candidate[:60]
    return (t[:40] or "untitled").strip()


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\-_. а-яА-ЯёЁ]+", "_", name).strip("_ ")
    return (cleaned or "project")[:80]


def _save_pipeline(state: dict) -> Path:
    fname = _safe_filename(state.get("название", "project")) + ".json"
    path = PROJECTS_DIR / fname
    serializable = {
        "название":  state.get("название"),
        "дата":      state.get("дата"),
        "завершен":  state.get("завершен"),
        "описание":  state.get("описание"),
        "статус":    state.get("статус"),
        "результаты": {
            k: {kk: vv for kk, vv in v.items() if kk != "step"}
            for k, v in state.get("шаги", {}).items()
        },
    }
    path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


async def _post_via(agent_name: str, chat_id: int, text: str) -> None:
    bot = BOTS.get(agent_name)
    if not bot:
        return
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        log.warning("post via %s failed: %s", agent_name, e)


async def _run_one_step(step_no: int, agent_name: str, brief: str,
                        chat_id: int, state: dict) -> None:
    meta = AGENTS_BY_NAME.get(agent_name)
    bot = BOTS.get(agent_name)
    aid = AGENT_IDS.get(agent_name)
    label_meta = meta.display if meta else agent_name

    if not (meta and bot and aid):
        # Agent not connected — skip with notice
        await _post_via("orchestrator", chat_id,
                        f"⏭ {label_meta} пока не подключён — пропускаем")
        state["шаги"][agent_name] = {"step": step_no, "skipped": True}
        return

    # Build context: project description + every previous step's result
    ctx_parts = [f"Описание проекта:\n{state['описание']}"]
    for prev_name, prev in state["шаги"].items():
        if prev.get("skipped"):
            continue
        prev_meta = AGENTS_BY_NAME.get(prev_name)
        prev_label = prev_meta.display if prev_meta else prev_name
        if "result" in prev:
            ctx_parts.append(f"\n--- {prev_label} ---\n{prev['result']}")
    ctx_parts.append(f"\n--- Твоя задача (Шаг {step_no}/6) ---\n{brief}")
    task_text = "\n".join(ctx_parts)

    label = f"{meta.emoji} {meta.display} — Шаг {step_no}/6:"
    try:
        placeholder = await bot.send_message(chat_id=chat_id,
                                             text=f"{label}\nработаю…")
    except Exception:
        placeholder = None

    try:
        result = await run_session(aid, task_text, on_chunk=None,
                                   agent_name=agent_name)
    except Exception as e:
        log.exception("pipeline step %s failed", agent_name)
        result = f"⚠️ Ошибка: {type(e).__name__}: {e}"

    result = _finalize(result)
    state["шаги"][agent_name] = {"step": step_no, "result": result}

    # Persist this step into the agent's memory store (project-scoped)
    received = "\n\n".join(f"--- {AGENTS_BY_NAME[n].display} ---\n{d['result']}"
                           for n, d in state["шаги"].items()
                           if n != agent_name and not d.get("skipped") and "result" in d)
    asyncio.create_task(record_pipeline_step(
        agent_name, chat_id,
        project=state.get("название", "untitled"),
        step_no=step_no,
        my_contribution=result,
        data_received=received,
        data_passed=result,
    ))

    final_text = f"{label}\n{result}\n\n✅ Передаю следующему..."
    await send_long(bot, chat_id, final_text, first_message=placeholder)


async def _run_pipeline(chat_id: int, project_text: str) -> None:
    name = _extract_project_name(project_text)
    state: dict = {
        "название":  name,
        "дата":      datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "описание":  project_text.strip(),
        "шаги":      {},
        "статус":    "running",
        "current_step": 0,
        "current_agent": None,
        "cancel":    asyncio.Event(),
    }
    ACTIVE_PIPELINES[chat_id] = state

    plan = (
        f"🚀 Новый проект: {name}\n\n"
        "План работы команды:\n"
        "1️⃣ 🔍 Милена — анализ ниши и конкурентов\n"
        "2️⃣ 📐 Александр — стратегия и контент-план\n"
        "3️⃣ ✍️ Алина — 3 первых поста\n"
        "4️⃣ 📊 Николай — KPI и метрики\n"
        "5️⃣ 🛡 Критик — проверка результатов\n"
        "6️⃣ 🎯 Андрей — итоговое резюме\n\n"
        "Поехали…"
    )
    await _post_via("orchestrator", chat_id, plan)

    try:
        for step_no, agent_name, brief in PIPELINE:
            if state["cancel"].is_set():
                break
            state["current_step"] = step_no
            state["current_agent"] = agent_name
            await _run_one_step(step_no, agent_name, brief, chat_id, state)
            await asyncio.sleep(2.0)

        # Step 6 — orchestrator final summary (rendered by content_writer agent
        # because the orchestrator agent's system prompt forces JSON output;
        # we still post via the orchestrator bot under "Андрей Оркестратор").
        if not state["cancel"].is_set():
            state["current_step"] = 6
            state["current_agent"] = "orchestrator"

            ctx = [f"Проект: {name}", f"Описание: {state['описание']}", ""]
            for n, d in state["шаги"].items():
                if d.get("skipped") or "result" not in d:
                    continue
                m = AGENTS_BY_NAME.get(n)
                ctx.append(f"--- {m.display if m else n} ---\n{d['result']}\n")

            ctx.append(
                "--- Задача (Шаг 6/6) ---\n"
                "Сформируй ИТОГ ПРОЕКТА в виде ОБЫЧНОГО ЧИТАЕМОГО ТЕКСТА "
                "(никакого JSON, никаких кодовых блоков). "
                "Используй ровно этот шаблон, заполни его по существу:\n\n"
                f"🎯 Андрей Оркестратор — Итог проекта {name}:\n\n"
                "📋 Что сделано:\n"
                "- Милена: 1–2 строки сути её анализа\n"
                "- Александр: 1–2 строки сути стратегии\n"
                "- Алина: 1–2 строки про посты\n"
                "- Николай: 1–2 строки про метрики\n\n"
                "✅ Готово к запуску:\n"
                "- конкретные действия списком\n\n"
                "➡️ Следующие шаги:\n"
                "- следующий шаг 1\n"
                "- следующий шаг 2\n"
                "- ...\n\n"
                "Если кого-то из агентов не было — пропусти его строку. "
                "Не добавляй вступлений и пояснений вне шаблона."
            )

            # Use content_writer (or fall back to strategist) — produces clean prose.
            renderer = "content_writer" if "content_writer" in AGENT_IDS else "strategist"
            renderer_id = AGENT_IDS.get(renderer)
            try:
                summary = (await run_session(renderer_id, "\n".join(ctx), on_chunk=None)
                           if renderer_id else "(renderer agent unavailable)")
            except Exception as e:
                log.exception("pipeline final failed")
                summary = f"⚠️ Ошибка: {type(e).__name__}: {e}"
            summary = _finalize(summary)
            state["шаги"]["orchestrator_final"] = {"step": 6, "result": summary}

            # Ensure the canonical header is on top regardless of what the model returned
            header_re = re.compile(r"^\s*🎯[^\n]*Итог проекта[^\n]*\n+", re.IGNORECASE)
            body_text = header_re.sub("", summary, count=1).strip()
            final_text = (f"🎯 Андрей Оркестратор — Итог проекта {name}:\n\n"
                          f"{body_text}")
            orch_bot = BOTS.get("orchestrator")
            if orch_bot:
                await send_long(orch_bot, chat_id, final_text)
            else:
                await _post_via("orchestrator", chat_id, final_text)

        state["статус"] = "stopped" if state["cancel"].is_set() else "completed"
        state["завершен"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        path = _save_pipeline(state)
        if state["cancel"].is_set():
            await _post_via("orchestrator", chat_id, "🛑 Pipeline остановлен.")
        await _post_via("orchestrator", chat_id,
                        f"💾 Сохранено: projects/{path.name}")

        # Drop a per-project profile into every participating agent's memory.
        if not state["cancel"].is_set():
            participants = [n for n, d in state["шаги"].items()
                            if not d.get("skipped") and "result" in d
                            and n in MEMORY_STORES]
            participants.append("orchestrator")  # always include the conductor
            project_summary = state["шаги"].get("orchestrator_final", {}).get(
                "result", "")
            for n in set(participants):
                asyncio.create_task(record_project_summary(
                    n, state.get("название", "untitled"), project_summary))
    finally:
        ACTIVE_PIPELINES.pop(chat_id, None)


async def _stop_pipeline(chat_id: int) -> bool:
    state = ACTIVE_PIPELINES.get(chat_id)
    if not state:
        return False
    state["cancel"].set()
    return True


def _format_status(state: dict) -> str:
    cur = state.get("current_step") or 0
    agent_key = state.get("current_agent")
    meta = AGENTS_BY_NAME.get(agent_key) if agent_key else None
    who = meta.display if meta else (agent_key or "—")
    done = [k for k, v in state.get("шаги", {}).items() if not v.get("skipped")]
    return (f"📋 Проект: {state.get('название', '—')}\n"
            f"Статус: {state.get('статус', '—')}\n"
            f"Шаг: {cur}/6 — сейчас: {who}\n"
            f"Завершено агентов: {len(done)}")


# ----------------------------------------------------------- group logic

async def _handle_group_message(meta: AgentMeta, agent_id: str, msg, user_text: str) -> None:
    """Group chat handler shared by all bots. Rules:
       - orchestrator: respond when no specialist is @mentioned, ignore other bots.
       - specialist:   respond ONLY when @mentioned;
                       from a bot — only if that bot is the orchestrator.
       - never respond to your own messages (avoid loops).
    """
    from_user = msg.from_user
    if from_user is None:
        return
    is_from_bot = bool(from_user.is_bot)
    from_at = ("@" + (from_user.username or "")).lower() if is_from_bot else ""

    my_at = TEAM_REGISTRY.get(meta.name, "").lower()
    orch_at = TEAM_REGISTRY.get("orchestrator", "").lower()
    specialist_ats = {
        TEAM_REGISTRY.get(a.name, "").lower()
        for a in AGENTS if a.name != "orchestrator"
    } - {""}

    # never answer own messages
    if is_from_bot and from_at == my_at:
        return

    mentions = _extract_mentions(msg)

    # ---------------- ORCHESTRATOR in group ----------------
    if meta.name == "orchestrator":
        # Don't react to other bots
        if is_from_bot:
            return
        # If user pinged a specific specialist, stay silent — that bot will answer
        if mentions & specialist_ats:
            return
        # If user pinged the orchestrator explicitly, strip its @ from text
        text_for_routing = _strip_mention(user_text, my_at) if my_at in mentions else user_text
        text_for_routing = text_for_routing.strip()
        if not text_for_routing:
            return

        try:
            routing_raw = await run_session(agent_id, text_for_routing,
                                            on_chunk=None,
                                            agent_name="orchestrator")
        except Exception as e:
            log.exception("orchestrator group routing failed")
            await msg.reply_text(f"🧭 ⚠️ Ошибка анализа: {type(e).__name__}: {e}")
            return

        routing = _try_parse_orchestrator_payload(routing_raw)
        if not routing:
            routing = _heuristic_route(text_for_routing)
        target_name = routing.get("agent") if routing else None
        if (not routing or target_name not in AGENT_IDS
                or target_name == "orchestrator"
                or target_name not in BOTS):
            await msg.reply_text("🧭 Не смог понять, какому агенту делегировать.")
            return

        target_meta = AGENTS_BY_NAME[target_name]

        task_text = (routing.get("task") or "").strip() or text_for_routing
        extras = []
        if routing.get("project"): extras.append(f"Project: {routing['project']}")
        if routing.get("priority"): extras.append(f"Priority: {routing['priority']}")
        if routing.get("context"): extras.append(f"Context: {routing['context']}")
        payload = task_text + ("\n\n" + "\n".join(extras) if extras else "")

        # Announce intent in the group, then call the target agent directly —
        # NO bot-to-bot @mention. Telegram does not deliver one bot's messages
        # to another bot, so we hand off in-process via execute_agent().
        await msg.reply_text(f"⚙️ Передаю задачу → {target_meta.display}…")
        await execute_agent(target_name, payload, msg.chat.id)
        return

    # ---------------- SPECIALIST in group ----------------
    # Must be @mentioned to respond
    if my_at not in mentions:
        return
    # If from bot, only the orchestrator can wake me
    if is_from_bot and from_at != orch_at:
        return

    # Build clean task text without my own @-mention
    task = _strip_mention(user_text, my_at).strip()
    if not task:
        return

    await msg.chat.send_action(ChatAction.TYPING)
    header = f"{meta.emoji} <b>{meta.title}</b>\n\n"
    thinking = await msg.reply_text(header + "<i>работаю…</i>", parse_mode="HTML")

    last_edit_at = 0.0
    last_sent = ""

    async def on_chunk(full: str):
        nonlocal last_edit_at, last_sent
        now = asyncio.get_event_loop().time()
        if now - last_edit_at < 1.5:
            return
        preview = full if len(full) < 3500 else full[-3500:]
        if preview == last_sent:
            return
        try:
            await thinking.edit_text(header + _html_escape(preview) + " ▌",
                                     parse_mode="HTML")
            last_sent = preview
            last_edit_at = now
        except Exception:
            pass

    try:
        final = await run_session(agent_id, task, on_chunk,
                                  agent_name=meta.name)
    except Exception as e:
        log.exception("specialist group session failed for %s", meta.name)
        await thinking.edit_text(header + f"⚠️ Ошибка: <code>{type(e).__name__}: {e}</code>",
                                 parse_mode="HTML")
        return

    final = _finalize(final)
    bot_for_send = BOTS.get(meta.name) or thinking.get_bot()
    await send_long(bot_for_send, msg.chat.id,
                    header + _html_escape(final),
                    first_message=thinking, parse_mode="HTML")
    asyncio.create_task(record_task(meta.name, msg.chat.id,
                                    task=task, result=final))


# ------------------------------------------------------- per-bot handlers

def make_handlers(meta: AgentMeta, agent_id: str):
    async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            f"{meta.emoji} <b>{meta.title}</b>\n\n{meta.short}\n\n"
            f"Команды: /help" + (" /team" if meta.name == "orchestrator" else ""),
            parse_mode="HTML",
        )

    async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"{meta.emoji} <b>{meta.title}</b>\n\n{meta.help}",
                                        parse_mode="HTML")

    async def team_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        lines = [f"{a.emoji} <b>{a.title}</b> — {TEAM_REGISTRY.get(a.name, '— не запущен')}"
                 for a in AGENTS]
        await update.message.reply_text("Команда:\n" + "\n".join(lines), parse_mode="HTML")

    async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            await _on_message_impl(update, ctx)
        except Exception as e:
            log.exception("[%s] unhandled error in on_message: %s",
                          meta.name, e)
            try:
                await update.message.reply_text(
                    f"⚠️ Внутренняя ошибка: {type(e).__name__}: {e}"[:300])
            except Exception:
                pass

    async def _on_message_impl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        user_text = (msg.text or "").strip()
        if not user_text:
            return

        is_group = msg.chat.type in ("group", "supergroup")
        from_user = msg.from_user
        sender = (from_user.username or from_user.first_name) if from_user else "?"
        log.info(
            "[%s] received: chat_type=%s chat=%s from=@%s len=%d preview=%r",
            meta.name, msg.chat.type, msg.chat.id, sender,
            len(user_text), user_text[:60],
        )
        if is_group and not _is_our_group(msg.chat):
            log.info("[%s] dropped: chat_id %s != GROUP_CHAT_ID %s",
                     meta.name, msg.chat.id, GROUP_CHAT_ID)
            return

        # ============== Project pipeline triggers (group only, orchestrator drives) ==============
        if is_group:
            from_user = msg.from_user
            if from_user and from_user.is_bot:
                pass  # never start pipeline from bot's own message
            else:
                if _is_stop_pipeline(user_text):
                    if meta.name == "orchestrator":
                        ok = await _stop_pipeline(msg.chat.id)
                        await msg.reply_text("🛑 Останавливаю pipeline…" if ok
                                              else "ℹ️ Активного проекта нет.")
                    return
                if _is_status_pipeline(user_text):
                    if meta.name == "orchestrator":
                        st = ACTIVE_PIPELINES.get(msg.chat.id)
                        await msg.reply_text(_format_status(st) if st
                                              else "ℹ️ Активного проекта нет.")
                    return
                if _is_project_start(user_text):
                    if meta.name == "orchestrator":
                        if msg.chat.id in ACTIVE_PIPELINES:
                            await msg.reply_text(
                                "⚠️ Уже идёт проект — напиши «стоп проект» чтобы прервать."
                            )
                            return
                        # Fire-and-forget so the handler returns quickly
                        asyncio.create_task(_run_pipeline(msg.chat.id, user_text))
                    return

        # ============== Feedback ("хорошо" / "переделай" / 👍 / 👎) ==============
        fb = _detect_feedback(user_text)
        if fb:
            from_user = msg.from_user
            if from_user and from_user.is_bot:
                return
            now = time.time()
            if is_group:
                # Only orchestrator confirms — but we route feedback to the last
                # agent that posted in this chat. Avoid one feedback line being
                # acked by every bot.
                if meta.name != "orchestrator":
                    return
                slot = LAST_TASK_GROUP.get(msg.chat.id)
                if slot and now - slot["ts"] < FEEDBACK_TTL_SEC:
                    asyncio.create_task(apply_feedback(
                        slot["agent_name"], slot["mem_id"], fb, user_text))
                    target_meta = AGENTS_BY_NAME.get(slot["agent_name"])
                    label = target_meta.display if target_meta else slot["agent_name"]
                    await msg.reply_text(
                        f"📝 Записал {('👍 положительный' if fb=='positive' else '👎 негативный')} "
                        f"отзыв для {label}.")
                else:
                    await msg.reply_text("ℹ️ Нечего комментировать — не нашёл свежей задачи.")
                return
            else:
                slot = LAST_TASK.get((meta.name, msg.chat.id))
                if slot and now - slot["ts"] < FEEDBACK_TTL_SEC:
                    asyncio.create_task(apply_feedback(
                        meta.name, slot["mem_id"], fb, user_text))
                    await msg.reply_text(
                        f"📝 Записал {('👍 положительный' if fb=='positive' else '👎 негативный')} "
                        f"отзыв.")
                else:
                    await msg.reply_text("ℹ️ Нечего комментировать — нет свежей задачи.")
                return

        # ============== Greeting trigger (handled once per message) ==============
        if _is_greeting(user_text):
            from_user = msg.from_user
            if from_user and from_user.is_bot:
                return  # ignore greetings coming from other bots
            if is_group:
                # Only orchestrator runs the team-greeting sequence; specialists stay quiet
                if meta.name == "orchestrator":
                    await _run_group_greeting(msg.chat.id)
                return
            else:
                # Private chat: each bot replies with its own greeting only
                greet = GREETINGS.get(meta.name)
                if greet:
                    await msg.reply_text(greet)
                return

        # ============== GROUP CHAT branch (non-greeting) ==============
        if is_group:
            await _handle_group_message(meta, agent_id, msg, user_text)
            return

        # ============== Below: PRIVATE chat ==============
        await msg.chat.send_action(ChatAction.TYPING)

        # ============== ORCHESTRATOR: route + handoff to target bot ==============
        if meta.name == "orchestrator":
            user_chat_id = update.message.chat_id
            thinking = await update.message.reply_text(
                "🧭 <i>анализирую задачу…</i>", parse_mode="HTML"
            )
            try:
                routing_raw = await run_session(agent_id, user_text, on_chunk=None,
                                                agent_name="orchestrator")
            except Exception as e:
                log.exception("orchestrator routing failed")
                await thinking.edit_text(
                    f"🧭 ⚠️ Ошибка анализа: <code>{type(e).__name__}: {e}</code>",
                    parse_mode="HTML",
                )
                return

            routing = _try_parse_orchestrator_payload(routing_raw)
            if not routing:
                routing = _heuristic_route(user_text)
            target_name = routing.get("agent") if routing else None
            if (not routing or target_name not in AGENT_IDS
                    or target_name == "orchestrator"
                    or target_name not in BOTS):
                fallback = routing_raw if len(routing_raw) < 3500 else routing_raw[:3500] + "…"
                await thinking.edit_text(
                    "🧭 <b>Не смог определить агента или нужный бот не запущен.</b>\n\n<pre>"
                    + _html_escape(fallback) + "</pre>",
                    parse_mode="HTML",
                )
                return

            target_meta = AGENTS_BY_NAME[target_name]
            target_id = AGENT_IDS[target_name]
            target_bot = BOTS[target_name]
            handle = TEAM_REGISTRY.get(target_name, target_name)

            # Build payload for the target agent
            task_text = (routing.get("task") or "").strip() or user_text
            extras = []
            if routing.get("project"): extras.append(f"Project: {routing['project']}")
            if routing.get("priority"): extras.append(f"Priority: {routing['priority']}")
            if routing.get("context"): extras.append(f"Context: {routing['context']}")
            payload = task_text + ("\n\n" + "\n".join(extras) if extras else "")

            # Try to open a message in the target bot's chat with the same user
            target_header = (f"{target_meta.emoji} <b>{target_meta.title}</b>\n"
                             f"<i>(задача от 🧭 Orchestrator)</i>\n\n")
            try:
                target_msg = await target_bot.send_message(
                    chat_id=user_chat_id,
                    text=target_header + "<i>работаю над задачей…</i>",
                    parse_mode="HTML",
                )
            except Exception as e:
                log.warning("cannot send to %s as %s: %s", user_chat_id, target_name, e)
                await thinking.edit_text(
                    f"🧭 Хотел делегировать → {handle}, но не могу написать тебе от его имени.\n\n"
                    f"Открой {handle}, нажми /start один раз, затем повтори задачу здесь.",
                    parse_mode="HTML",
                )
                return

            # Tell the user in orchestrator chat where to go
            await thinking.edit_text(
                f"✅ Делегирую задачу → {handle}\n"
                f"Иди туда — он/она уже работает над задачей.",
                parse_mode="HTML",
            )

            # Stream target agent's response into target_msg
            last_edit_at = 0.0
            last_sent_text = ""

            async def on_chunk_target(full: str):
                nonlocal last_edit_at, last_sent_text
                now = asyncio.get_event_loop().time()
                if now - last_edit_at < 1.2:
                    return
                preview = full if len(full) < 3500 else full[-3500:]
                if preview == last_sent_text:
                    return
                try:
                    await target_bot.edit_message_text(
                        text=target_header + _html_escape(preview) + " ▌",
                        chat_id=user_chat_id, message_id=target_msg.message_id,
                        parse_mode="HTML",
                    )
                    last_sent_text = preview
                    last_edit_at = now
                except Exception:
                    pass

            try:
                final = await run_session(target_id, payload, on_chunk_target,
                                          agent_name=target_name)
            except Exception as e:
                log.exception("target session failed for %s", target_name)
                try:
                    await target_bot.edit_message_text(
                        text=target_header + f"⚠️ Ошибка: <code>{type(e).__name__}: {e}</code>",
                        chat_id=user_chat_id, message_id=target_msg.message_id,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
                return

            final = _finalize(final)
            await send_long(target_bot, user_chat_id,
                            target_header + _html_escape(final),
                            first_message=target_msg, parse_mode="HTML")
            return

        # ============== Regular bot path (specialist agents) ==============
        header = f"{meta.emoji} <b>{meta.title}</b>\n\n"
        thinking = await update.message.reply_text(header + "<i>думаю…</i>",
                                                   parse_mode="HTML")

        last_edit_at = 0.0
        last_sent_text = ""

        async def on_chunk(full: str):
            nonlocal last_edit_at, last_sent_text
            now = asyncio.get_event_loop().time()
            if now - last_edit_at < 1.2:
                return
            preview = full if len(full) < 3500 else full[-3500:]
            if preview == last_sent_text:
                return
            try:
                await thinking.edit_text(header + _html_escape(preview) + " ▌",
                                         parse_mode="HTML")
                last_sent_text = preview
                last_edit_at = now
            except Exception:
                pass

        review_footer = ""
        try:
            if (meta.name in AGENTS_REVIEWED_BY_CRITIC
                    and AGENT_IDS.get("critic")):
                async def _status(s: str):
                    try:
                        await thinking.edit_text(
                            header + f"<i>{_html_escape(s)}</i>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                final, history = await run_with_critic_review(
                    meta.name, agent_id, user_text, status_cb=_status)
                review_footer = _format_review_footer(history)
            else:
                final = await run_session(agent_id, user_text, on_chunk,
                                          agent_name=meta.name)
        except Exception as e:
            log.exception("session failed for %s", meta.name)
            await thinking.edit_text(header + f"⚠️ Ошибка: <code>{type(e).__name__}: {e}</code>",
                                     parse_mode="HTML")
            return

        final = _finalize(final) + review_footer
        bot_for_send = BOTS.get(meta.name) or thinking.get_bot()
        await send_long(bot_for_send, msg.chat.id,
                        header + _html_escape(final),
                        first_message=thinking, parse_mode="HTML")
        asyncio.create_task(record_task(meta.name, msg.chat.id,
                                        task=user_text, result=final))

    handlers = [
        CommandHandler("start", start_cmd),
        CommandHandler("help", help_cmd),
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_message),
    ]
    if meta.name == "orchestrator":
        handlers.insert(2, CommandHandler("team", team_cmd))
    return handlers


_AGENT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    # order matters — more specific first
    ("dev_agent",      ("dev", "разработчик", "программист", "код", "бот", "mini app",
                         "минипп", "миниапп", "deploy", "michael", "миша", "михаил",
                         "fastapi", "react", "next.js", "telegram bot")),
    ("content_writer", ("писатель", "копирайтер", "пост", "текст", "хук", "креатив",
                         "оффер", "лендинг", "writer", "copywriter", "алина",
                         "сторителлинг", "сценарий", "caption", "статья")),
    ("researcher",     ("исследов", "конкурент", "анализ ниши", "researcher",
                         "милена", "ниша", "тренд", "data", "статистика",
                         "рынок", "market")),
    ("strategist",     ("стратег", "стратегия", "стратегию", "strategy", "план",
                         "плана", "плану", "планирование", "roadmap", "gtm", "канвас",
                         "александр", "позиционирование", "запуск", "запускаем",
                         "запустить", "okr", "монетизация", "монетизацию",
                         "контент-план", "контент план", "lean canvas")),
    ("analyst",        ("метрик", "kpi", "analytics", "аналитик", "николай",
                         "roas", "cpm", "cpc", "ltv", "cac", "конверс",
                         "воронк", "funnel", "report", "dashboard")),
    ("sales_agent",    ("продаж", "sale", "виктор", "возражени", "оффер продаж",
                         "клиент", "ltv", "продай", "скрипт продаж", "лид",
                         "прогрев", "вебинар")),
    ("critic",         ("проверь", "проверка", "критик", "review", "qa",
                         "ошибк", "оцени")),
]


def _heuristic_route(text: str) -> Optional[dict]:
    """Last-resort routing when orchestrator failed to emit JSON.
    Picks the agent whose keywords best match the user's text.
    Uses Cyrillic-aware word boundaries so that 'код' does not match
    inside 'вайбкодинг'."""
    s = (text or "").lower()
    if not s.strip():
        return None
    best: tuple[int, Optional[str]] = (0, None)
    for agent, kws in _AGENT_KEYWORDS:
        score = 0
        for kw in kws:
            if " " in kw:
                # multi-word phrases match as substrings (rare false-positive risk)
                if kw in s:
                    score += 1
            else:
                # left word-boundary only — match as a stem so that
                # "конкурент" matches "конкурентов" but not "вайбкодинг"
                pat = rf"(?<![\wа-яё]){re.escape(kw)}"
                if re.search(pat, s):
                    score += 1
        if score > best[0]:
            best = (score, agent)
    if best[1]:
        return {"agent": best[1], "task": text.strip(),
                "project": None, "priority": "medium", "context": ""}
    return None


def _try_parse_orchestrator_payload(text: str) -> Optional[dict]:
    """Extract orchestrator's routing JSON. Returns dict or None.

    Tolerance ladder:
      1. Plain JSON in the response.
      2. JSON with single quotes (Pythonic).
      3. JSON with stray prose around it — extract from { to last }.
      4. Multiple {…} blocks — try each.
      5. If still nothing — DO NOT guess here; caller may fall back to
         _heuristic_route(original user text).
    """
    if not text:
        return None
    raw = text.strip()
    candidates: list[str] = [raw]
    if "{" in raw and "}" in raw:
        candidates.append(raw[raw.find("{"): raw.rfind("}") + 1])
    # All standalone {...} blocks
    for m in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", raw, re.DOTALL):
        candidates.append(m.group(0))

    for c in candidates:
        for variant in (c, c.replace("'", '"')):
            try:
                obj = json.loads(variant)
            except Exception:
                continue
            if isinstance(obj, dict):
                a = obj.get("agent")
                # Accept Russian display names too — normalise to canonical id
                if isinstance(a, str):
                    canonical = _canonicalize_agent(a)
                    if canonical:
                        obj["agent"] = canonical
                        return obj
    return None


_AGENT_ALIAS: dict[str, str] = {
    # canonical
    "researcher": "researcher", "strategist": "strategist",
    "content_writer": "content_writer", "dev_agent": "dev_agent",
    "analyst": "analyst", "sales_agent": "sales_agent",
    "critic": "critic",
    # short / human aliases
    "writer": "content_writer", "content writer": "content_writer",
    "копирайтер": "content_writer", "алина": "content_writer",
    "dev": "dev_agent", "developer": "dev_agent",
    "разработчик": "dev_agent", "михаил": "dev_agent", "миша": "dev_agent",
    "milena": "researcher", "милена": "researcher",
    "research": "researcher", "researcher_agent": "researcher",
    "александр": "strategist", "alex": "strategist", "стратег": "strategist",
    "николай": "analyst", "nikolay": "analyst", "аналитик": "analyst",
    "виктор": "sales_agent", "victor": "sales_agent",
    "sales": "sales_agent", "продажник": "sales_agent",
    "qa": "critic", "критик": "critic", "reviewer": "critic",
}


def _canonicalize_agent(name: str) -> Optional[str]:
    if not name:
        return None
    return _AGENT_ALIAS.get(name.strip().lower())


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_table_to_bullets(block: list[str]) -> list[str]:
    """Turn a markdown table block into '• key: value' bullets."""
    sep_cell = re.compile(r"^\s*:?-{3,}:?\s*$")
    rows: list[list[str]] = []
    for ln in block:
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        rows.append(cells)
    rows = [r for r in rows if not (r and all(sep_cell.match(c or "") for c in r))]
    if not rows:
        return []
    headers = rows[0]
    data = rows[1:] if len(rows) > 1 else []
    bullets: list[str] = []
    if not data:
        return [f"• {h}" for h in headers if h]
    for r in data:
        if len(r) == 2 and len(headers) == 2 and r[0]:
            bullets.append(f"• {r[0]}: {r[1]}")
        else:
            pairs = [f"{h}: {v}" for h, v in zip(headers, r) if v and h]
            bullets.append("• " + " — ".join(pairs) if pairs else "•")
    return bullets


def _convert_md_tables(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("|") and line.count("|") >= 2:
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append(lines[i])
                i += 1
            out.extend(_md_table_to_bullets(block))
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def clean_for_telegram(text: str) -> str:
    """Strip markdown so plain Telegram messages render cleanly."""
    if not text:
        return text
    s = text

    # 1. Fenced code blocks ``` ... ``` → keep contents, drop fences/lang tag
    s = re.sub(r"```[a-zA-Z0-9_+\-]*\n?(.*?)```", r"\1", s, flags=re.DOTALL)

    # 2. Tables → bullet list
    s = _convert_md_tables(s)

    # 3. Horizontal rules (---, ===, ***) on their own line
    s = re.sub(r"^[ \t]*[-=*_]{3,}[ \t]*$", "", s, flags=re.MULTILINE)

    # 4. ATX headings (# .. ###### ...) → keep title text only
    s = re.sub(r"^[ \t]*#{1,6}[ \t]+", "", s, flags=re.MULTILINE)

    # 5. Bold / italic / inline code
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s, flags=re.DOTALL)
    s = re.sub(r"__(.+?)__",     r"\1", s, flags=re.DOTALL)
    s = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\*)", r"\1", s)
    s = re.sub(r"(?<![_\w])_([^_\n]+)_(?!_)",     r"\1", s)
    s = re.sub(r"`([^`\n]+)`", r"\1", s)

    # 6. Links: [text](url) → text;  bare [text] → text
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"\[([^\]\n]+)\]", r"\1", s)

    # 7. Collapse multiple blank lines
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)

    return s.strip()


def _finalize(text: str) -> str:
    """Pipeline used before posting agent output to Telegram:
    1) extract readable string from accidental JSON  2) strip markdown."""
    return clean_for_telegram(_clean_agent_output(text))


def split_message(text: str, max_length: int = 4000) -> list[str]:
    """Split `text` into chunks ≤ max_length, preferring paragraph boundaries.

    Tries in order: blank-line break (\\n\\n), single newline, sentence end
    (. ?!), word boundary, then a hard cut. Returns at least one chunk.
    """
    text = (text or "").rstrip()
    if not text:
        return [""]
    if len(text) <= max_length:
        return [text]

    parts: list[str] = []
    remaining = text
    half = max_length // 2

    while len(remaining) > max_length:
        window = remaining[:max_length]
        cut = window.rfind("\n\n")
        if cut < half:
            cut = window.rfind("\n")
        if cut < half:
            for marker in (". ", "! ", "? ", "; "):
                pos = window.rfind(marker)
                if pos > cut:
                    cut = pos + len(marker) - 1   # include the punctuation
        if cut < half:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = max_length  # last resort: hard cut

        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    if remaining:
        parts.append(remaining)
    return parts


async def send_long(bot, chat_id: int, text: str, *,
                    first_message=None,
                    parse_mode: Optional[str] = None,
                    page_threshold: int = 2) -> None:
    """Post `text` as one or more Telegram messages.

    - If `first_message` is given, edits that message with the first chunk;
      otherwise sends a fresh message.
    - When the result splits into >= `page_threshold` chunks, every chunk
      is prefixed with "📄 Часть K/N:".
    - 0.5 s pause between successive messages.
    - On HTML parse failure, falls back to plain text without raising.
    """
    parts = split_message(text)
    n = len(parts)

    def label_for(idx: int) -> str:
        return f"📄 Часть {idx}/{n}:\n\n" if n >= page_threshold else ""

    async def _send_or_edit(msg_text: str, edit_target):
        if edit_target is not None:
            try:
                await bot.edit_message_text(
                    text=msg_text, chat_id=chat_id,
                    message_id=edit_target.message_id,
                    parse_mode=parse_mode,
                )
                return True
            except Exception:
                # Try plain (parse_mode might be the issue) before falling
                # back to a fresh send.
                try:
                    await bot.edit_message_text(
                        text=msg_text, chat_id=chat_id,
                        message_id=edit_target.message_id,
                    )
                    return True
                except Exception:
                    pass
        try:
            await bot.send_message(chat_id=chat_id, text=msg_text,
                                   parse_mode=parse_mode)
            return True
        except Exception:
            try:
                await bot.send_message(chat_id=chat_id, text=msg_text)
                return True
            except Exception as e:
                log.warning("send_long: failed to deliver chunk: %s", e)
                return False

    await _send_or_edit(label_for(1) + parts[0], first_message)
    for i, chunk in enumerate(parts[1:], start=2):
        await asyncio.sleep(0.5)
        await _send_or_edit(label_for(i) + chunk, None)


def _clean_agent_output(text: str) -> str:
    """If an agent returns the orchestrator-style routing JSON or any JSON blob
    by mistake, surface a human-readable string instead of the raw object.
    Strips ```json ... ``` fences too. Falls back to the original text."""
    if not text:
        return text
    s = text.strip()
    # Strip code fences
    fence = re.match(r"^```(?:json)?\s*(.+?)\s*```$", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    # Try parse if it looks like a JSON object
    if s.startswith("{") and s.endswith("}"):
        for variant in (s, s.replace("'", '"')):
            try:
                obj = json.loads(variant)
            except Exception:
                continue
            if not isinstance(obj, dict):
                break
            # Prefer the most useful textual fields, in order
            for key in ("task", "result", "text", "content", "summary",
                        "answer", "message"):
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            # Else flatten remaining string fields
            parts = [f"{k}: {v}" for k, v in obj.items()
                     if isinstance(v, str) and v.strip()]
            if parts:
                return "\n".join(parts)
            break
    return text


def _extract_mentions(message) -> set[str]:
    """Return set of '@username' (lower-case) mentioned in message via entities."""
    out: set[str] = set()
    text = message.text or ""
    for ent in (message.entities or []):
        if ent.type == "mention":
            out.add(text[ent.offset : ent.offset + ent.length].lower())
    return out


def _strip_mention(text: str, mention: str) -> str:
    """Remove '@username' (case-insensitive) from text."""
    import re
    return re.sub(re.escape(mention), "", text, flags=re.IGNORECASE).strip()


def _is_our_group(chat) -> bool:
    if chat.type not in ("group", "supergroup"):
        return False
    if GROUP_CHAT_ID and str(chat.id) != GROUP_CHAT_ID:
        return False
    return True


# ----------------------------------------------------------------- runner

async def _lazy_register_username(meta: "AgentMeta", bot) -> None:
    """Fetch the bot's @username in the background with infinite retry.

    The username is only used for /team listings, group greetings and
    delegation messages. It's fine if it shows up a few seconds after
    startup — polling is already running."""
    delay = 5
    while True:
        try:
            me = await bot.get_me()
            TEAM_REGISTRY[meta.name] = f"@{me.username}"
            log.info("bot ready: %s -> @%s", meta.name, me.username)
            return
        except Exception as e:
            log.warning("get_me %s failed: %s — retry in %ds", meta.name, e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


async def build_bot(meta: AgentMeta, token: str, agent_id: str) -> Application:
    """Build a bot Application without any network calls.

    Telegram polling will lazily establish the connection on its own.
    `get_me()` runs in a background task and updates TEAM_REGISTRY when
    the network cooperates — never blocks the main startup."""
    from telegram.request import HTTPXRequest
    req = HTTPXRequest(connect_timeout=30, read_timeout=30,
                       write_timeout=30, pool_timeout=30)
    upd_req = HTTPXRequest(connect_timeout=30, read_timeout=30,
                           write_timeout=30, pool_timeout=30)
    app = (ApplicationBuilder()
           .token(token)
           .request(req)
           .get_updates_request(upd_req)
           .build())
    for h in make_handlers(meta, agent_id):
        app.add_handler(h)
    await app.initialize()       # local-only: sets up handlers, no network
    BOTS[meta.name] = app.bot
    asyncio.create_task(_lazy_register_username(meta, app.bot))
    log.info("bot scheduled: %s (agent=%s)", meta.name, agent_id)
    return app


async def main() -> int:
    global ANTHROPIC, ENVIRONMENT_ID
    cfg = load_config()
    ENVIRONMENT_ID = cfg.get("environment_id")
    if not ENVIRONMENT_ID:
        print("ERROR: agents_config.json missing environment_id", file=sys.stderr)
        return 1

    ANTHROPIC = make_anthropic()
    AGENT_IDS.update(cfg["agents"])
    MEMORY_STORES.update(cfg.get("memory_stores") or {})
    if MEMORY_STORES:
        log.info("memory stores loaded: %d", len(MEMORY_STORES))

    apps: list[tuple[AgentMeta, Application]] = []
    skipped: list[str] = []

    # Build all bot Applications. No network calls happen here —
    # username lookup runs in background tasks (see build_bot).
    for meta in AGENTS:
        token = (os.environ.get(meta.token_env) or "").strip()
        agent_id = cfg["agents"].get(meta.name)
        if not token:
            skipped.append(f"{meta.name} (no {meta.token_env})")
            continue
        if not agent_id:
            skipped.append(f"{meta.name} (no agent_id in config)")
            continue
        try:
            app = await build_bot(meta, token, agent_id)
            apps.append((meta, app))
        except Exception as e:
            skipped.append(f"{meta.name} (init failed: {e})")

    if not apps:
        print("No bots to run. Skipped:", skipped, file=sys.stderr)
        return 1

    print("\n=== Running bots ===")
    for meta, _ in apps:
        print(f"  {meta.emoji} {meta.name:<16} {TEAM_REGISTRY.get(meta.name, '?')}")
    if skipped:
        print("Skipped:")
        for s in skipped:
            print(f"  - {s}")
    print("Press Ctrl+C to stop.\n")

    # Start polling on all apps in parallel
    for _, app in apps:
        await app.updater.start_polling(drop_pending_updates=True)
        await app.start()

    try:
        # Block forever
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for _, app in apps:
            try:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
