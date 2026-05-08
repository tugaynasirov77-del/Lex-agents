# Lex Agents

Telegram multi-agent team backed by Claude Managed Agents.
Run as a Railway worker.

## Stack
- Python 3.11
- `anthropic` SDK (Managed Agents beta)
- `python-telegram-bot` v21 (async)

## Required env vars
See `.env.example` for the full list. **Never commit `.env`.**

```
ANTHROPIC_API_KEY=
TELEGRAM_ORCHESTRATOR_TOKEN=
TELEGRAM_RESEARCHER_TOKEN=
TELEGRAM_STRATEGIST_TOKEN=
TELEGRAM_WRITER_TOKEN=
TELEGRAM_DEV_TOKEN=
TELEGRAM_ANALYST_TOKEN=
TELEGRAM_SALES_TOKEN=
TELEGRAM_CRITIC_TOKEN=
GROUP_CHAT_ID=
```

## One-time setup (already done if `agents_config.json` is populated)
```bash
python setup_agents.py            # creates 8 Managed Agents + cloud env
python update_prompts.py          # writes system prompts
python setup_memory_stores.py     # one Memory Store per agent
python setup_skills.py            # uploads 18 custom Skills + attaches
```

## Run
```bash
python multi_bot.py
```

## Railway deployment
1. New project → Deploy from GitHub repo.
2. Set all env vars from the list above in the Railway dashboard.
3. Railway picks up `Procfile` / `railway.json` automatically.
