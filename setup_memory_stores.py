"""Create a Memory Store per agent and save IDs to agents_config.json."""
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from anthropic import Anthropic

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env", override=True)

BETA_HEADER = "managed-agents-2026-04-01"

AGENT_NAMES = ["orchestrator", "researcher", "strategist", "content_writer",
               "dev_agent", "analyst", "sales_agent", "critic"]

DESCRIPTIONS: dict[str, str] = {
    "orchestrator":   "Память Андрея Оркестратора: история проектов, делегирований, обратная связь.",
    "researcher":     "Память Милены: история анализов ниш, конкурентов, профили проектов.",
    "strategist":     "Память Александра: стратегии, контент-планы, профили проектов.",
    "content_writer": "Память Алины: контент-история, стиль клиентов, профили проектов.",
    "dev_agent":      "Память Михаила: технические решения, стек проектов.",
    "analyst":        "Память Николая: метрики проектов, бенчмарки, аномалии.",
    "sales_agent":    "Память Виктора: возражения, скрипты, успешные и провальные сделки.",
    "critic":         "Память Критика: типичные проблемы, замеченные паттерны.",
}


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    client = Anthropic(api_key=api_key,
                       default_headers={"anthropic-beta": BETA_HEADER},
                       timeout=120.0, max_retries=3)

    cfg_path = HERE / "agents_config.json"
    cfg = json.loads(cfg_path.read_text())
    stores: dict = dict(cfg.get("memory_stores") or {})

    for name in AGENT_NAMES:
        if stores.get(name):
            print(f"{name:<16}  already has store {stores[name]} — skip")
            continue
        store = client.beta.memory_stores.create(
            name=f"agent-{name}",
            description=DESCRIPTIONS[name],
        )
        stores[name] = store.id
        print(f"{name:<16}  created  {store.id}")

    cfg["memory_stores"] = stores
    cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    print(f"\nSaved → {cfg_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
