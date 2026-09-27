"""Add and remove only the harness-owned section of Codex AGENTS.md."""

from pathlib import Path


START = "<!-- BEGIN jev-codex-harness managed -->"
END = "<!-- END jev-codex-harness managed -->"


def section(repo: Path, codex_home: Path) -> str:
    template = (repo / "integrations/jev/global/AGENTS.md.template").read_text(encoding="utf-8")
    rendered = template.replace("{{JEV_UI_PATH}}", str(codex_home / "jev-global/ui.mjs"))
    if rendered.count(START) != 1 or rendered.count(END) != 1 or "{{" in rendered:
        raise ValueError("Invalid Jev AGENTS template")
    return rendered.rstrip("\n")


def merge_agents(existing: str, repo: Path, codex_home: Path) -> str:
    managed = section(repo, codex_home)
    suffix = "\n" + managed + "\n"
    if START in existing or END in existing:
        if managed_section(existing) == managed:
            return existing
        raise ValueError("Existing Jev AGENTS section differs; migration requires review")
    return existing + suffix


def managed_section(current: str) -> str | None:
    if current.count(START) != 1 or current.count(END) != 1:
        return None
    start = current.index(START)
    end = current.index(END) + len(END)
    if (start >= end or start == 0 or current[start - 1] != "\n"
            or current[end:end + 1] != "\n"):
        return None
    return current[start:end]


def remove_agents(current: str, repo: Path, codex_home: Path) -> str:
    managed = section(repo, codex_home)
    if managed_section(current) != managed:
        raise ValueError("Managed Jev AGENTS section changed; refusing rollback")
    start = current.index(START)
    end = start + len(managed)
    if start == 0 or current[start - 1] != "\n" or current[end:end + 1] != "\n":
        raise ValueError("Managed Jev AGENTS section boundary changed; refusing rollback")
    return current[:start - 1] + current[end + 1:]
