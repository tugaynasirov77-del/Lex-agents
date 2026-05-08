"""Provision 8 Managed Agents + 1 cloud Environment via Anthropic API (beta)."""
import json
import os
import sys
from pathlib import Path

from anthropic import Anthropic

BETA_HEADER = "managed-agents-2026-04-01"

AGENT_NAMES = [
    "orchestrator",
    "researcher",
    "strategist",
    "content_writer",
    "dev_agent",
    "analyst",
    "sales_agent",
    "critic",
]

MODEL_OVERRIDES = {
    "dev_agent": "claude-opus-4-7",
    "critic": "claude-haiku-4-5-20251001",
}
DEFAULT_MODEL = "claude-sonnet-4-6"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")


def main() -> int:
    load_env_file(Path(__file__).with_name(".env"))
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set (env or .env file).", file=sys.stderr)
        return 1

    client = Anthropic(api_key=api_key, default_headers={"anthropic-beta": BETA_HEADER})

    print("Creating environment...")
    environment = client.beta.environments.create(
        name="multiagent-team-env",
        config={"type": "cloud", "networking": {"type": "unrestricted"}},
    )
    print(f"  environment_id = {environment.id}")

    agents_created: dict[str, dict[str, str]] = {}
    for name in AGENT_NAMES:
        model = MODEL_OVERRIDES.get(name, DEFAULT_MODEL)
        print(f"Creating agent {name} ({model})...")
        agent = client.beta.agents.create(
            name=name,
            model=model,
            system=f"You are {name}. Awaiting system prompt configuration.",
            tools=[{"type": "agent_toolset_20260401"}],
        )
        agents_created[name] = {"id": agent.id, "model": model}

    config = {
        "agents": {n: a["id"] for n, a in agents_created.items()},
        "environment_id": environment.id,
    }
    out_path = Path(__file__).with_name("agents_config.json")
    out_path.write_text(json.dumps(config, indent=2))
    print(f"\nSaved config -> {out_path}")

    print("\n{:<16} {:<48} {}".format("NAME", "ID", "MODEL"))
    print("-" * 90)
    for name, info in agents_created.items():
        print("{:<16} {:<48} {}".format(name, info["id"], info["model"]))
    print("{:<16} {:<48} {}".format("(environment)", environment.id, "-"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
