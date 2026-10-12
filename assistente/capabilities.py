import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

MAX_SKILL_FILE_CHARS = 16_000
MAX_ACTIVE_SKILLS = 3
MAX_ACTIVE_SKILL_CHARS = 20_000
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_-]{2,}")
_STOP_WORDS = {
    "and",
    "are",
    "com",
    "como",
    "das",
    "de",
    "dos",
    "for",
    "from",
    "into",
    "para",
    "por",
    "que",
    "the",
    "uma",
    "use",
    "when",
    "with",
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    instructions: str
    path: Path


def _tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return {token for token in _TOKEN.findall(normalized) if token not in _STOP_WORDS}


def _read_skill(path: Path, allowed_root: Path) -> Skill | None:
    if path.is_symlink() or not path.is_file():
        return None
    resolved_root = allowed_root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return None
    content = path.read_text(encoding="utf-8")
    if len(content) > MAX_SKILL_FILE_CHARS:
        return None
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration:
        return None
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"name", "description"}:
            metadata[key.strip()] = value.strip().strip("\"'")
    name = metadata.get("name", "")
    description = metadata.get("description", "")
    if not name or not description:
        return None
    instructions = "\n".join(lines[end + 1 :]).strip()
    if not instructions:
        return None
    return Skill(name, description, instructions, resolved_path)


def discover_skills(project_root: Path) -> list[Skill]:
    skills: list[Skill] = []
    seen_names: set[str] = set()
    for skills_root in (project_root / "skills", project_root / ".github" / "skills"):
        if skills_root.is_symlink() or not skills_root.is_dir():
            continue
        resolved_root = skills_root.resolve()
        try:
            resolved_root.relative_to(project_root.resolve())
        except ValueError:
            continue
        for skill_directory in sorted(skills_root.iterdir()):
            if skill_directory.is_symlink() or not skill_directory.is_dir():
                continue
            skill_file = skill_directory / "SKILL.md"
            skill = _read_skill(skill_file, resolved_root)
            if skill is None or skill.name in seen_names:
                continue
            seen_names.add(skill.name)
            skills.append(skill)
    return skills


def select_skills(message: str, skills: list[Skill]) -> list[Skill]:
    request_tokens = _tokens(message)
    ranked: list[tuple[int, Skill]] = []
    for skill in skills:
        name_tokens = _tokens(skill.name.replace("-", " "))
        description_tokens = _tokens(skill.description)
        name_overlap = len(request_tokens & name_tokens)
        description_overlap = len(request_tokens & description_tokens)
        score = name_overlap * 3 + description_overlap
        if score >= 2:
            ranked.append((score, skill))
    ranked.sort(key=lambda item: (-item[0], item[1].name.casefold(), str(item[1].path)))

    selected: list[Skill] = []
    used_chars = 0
    for _, skill in ranked:
        size = len(skill.instructions)
        if len(selected) >= MAX_ACTIVE_SKILLS:
            break
        if used_chars + size > MAX_ACTIVE_SKILL_CHARS:
            continue
        selected.append(skill)
        used_chars += size
    return selected


def skills_inventory(skills: list[Skill]) -> str:
    if not skills:
        return "Nenhuma Skill de projeto válida encontrada. Locais verificados: skills/ e .github/skills/."
    return "\n".join(
        f"- {skill.name}: {skill.description} (fonte: {skill.path})" for skill in skills
    )
