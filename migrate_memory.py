#!/usr/bin/env python3
"""
migrate_memory.py — One-shot migration: memory.md → global_memory.json

Run once after deploying the new memory layer:
    python3 migrate_memory.py

Reads:  /tmp/tg_agent/memory.md  (old format: "- [date] fact" bullet points)
Writes: /tmp/tg_agent/global_memory.json
Safe: never overwrites existing global_memory.json data, only appends missing facts.
"""

import json
import re
import pathlib

OLD_FILE = pathlib.Path("/tmp/tg_agent/memory.md")
NEW_FILE = pathlib.Path("/tmp/tg_agent/global_memory.json")


def main() -> None:
    if not OLD_FILE.exists():
        print("No old memory.md found — nothing to migrate.")
        return

    old_facts: list[str] = []
    for line in OLD_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cleaned = re.sub(r'^[-*]\s*\[.*?\]\s*', '', line).strip()
            if cleaned:
                old_facts.append(cleaned)

    if not old_facts:
        print("memory.md is empty — nothing to migrate.")
        return

    # Load or init new JSON
    mem: dict = {}
    if NEW_FILE.exists():
        try:
            mem = json.loads(NEW_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            corrupt_backup = NEW_FILE.with_suffix(".corrupt.json")
            NEW_FILE.rename(corrupt_backup)
            print(f"Warning: existing global_memory.json was corrupt — saved as {corrupt_backup.name}, reinitializing.")

    # Merge: append facts not already present
    prefs: list = mem.get("user_profile", {}).get("preferences", [])
    added = 0
    for fact in old_facts:
        if fact not in prefs:
            prefs.append(fact)
            added += 1

    # Ensure full schema
    mem.setdefault("user_profile", {})
    mem["user_profile"]["preferences"] = prefs
    mem["user_profile"].setdefault("os", "")
    mem["user_profile"].setdefault("tools", [])
    mem.setdefault("project_state", {
        "current_goal": "",
        "milestones": [],
        "last_technical_decision": "",
    })
    mem.setdefault("short_term_context", "")

    NEW_FILE.parent.mkdir(parents=True, exist_ok=True)
    NEW_FILE.write_text(json.dumps(mem, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Migration complete: {added} new facts written to {NEW_FILE}")


if __name__ == "__main__":
    main()
