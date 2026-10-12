import tempfile
import unittest
from pathlib import Path

from assistente.capabilities import (
    MAX_ACTIVE_SKILLS,
    MAX_SKILL_FILE_CHARS,
    discover_skills,
    select_skills,
)


def write_skill(root: Path, folder: str, name: str, description: str, instructions: str) -> Path:
    directory = root / folder / name
    directory.mkdir(parents=True)
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{instructions}\n",
        encoding="utf-8",
    )
    return path


class SkillDiscoveryTests(unittest.TestCase):
    def test_discovers_project_skills_from_both_supported_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_skill(root, "skills", "pytest", "Run Python tests", "Run focused pytest.")
            write_skill(
                root,
                ".github/skills",
                "release-notes",
                "Write release notes",
                "Summarize changes.",
            )

            skills = discover_skills(root)

        self.assertEqual([skill.name for skill in skills], ["pytest", "release-notes"])

    def test_skips_symlinks_and_files_that_escape_project(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            write_skill(outside, "skills", "outside", "Outside skill", "Must not load.")
            (root / "skills").mkdir()
            (root / "skills" / "linked").symlink_to(outside / "skills", target_is_directory=True)
            write_skill(root, "skills", "safe", "Safe skill", "Load this.")
            (root / "skills" / "safe" / "SKILL.md").unlink()
            (root / "skills" / "safe" / "SKILL.md").symlink_to(
                outside / "skills" / "outside" / "SKILL.md"
            )

            skills = discover_skills(root)

        self.assertEqual(skills, [])

    def test_skips_oversized_and_malformed_skills(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            oversized = write_skill(
                root,
                "skills",
                "oversized",
                "Oversized content",
                "x" * MAX_SKILL_FILE_CHARS,
            )
            malformed = root / "skills" / "malformed"
            malformed.mkdir()
            (malformed / "SKILL.md").write_text("# No frontmatter", encoding="utf-8")

            skills = discover_skills(root)
            oversized_bytes = oversized.stat().st_size

        self.assertEqual(skills, [])
        self.assertGreater(oversized_bytes, MAX_SKILL_FILE_CHARS)

    def test_selects_relevant_skills_deterministically_with_a_small_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("python-tests", "pytest-coverage", "python-lint", "cloud-deploy"):
                write_skill(
                    root,
                    "skills",
                    name,
                    "Python pytest testing coverage"
                    if "python" in name or "pytest" in name
                    else "Cloud deployment",
                    f"Instructions for {name}.",
                )

            skills = discover_skills(root)
            first = select_skills("Write Python pytest tests and coverage", skills)
            second = select_skills("Write Python pytest tests and coverage", skills)

        self.assertEqual([skill.name for skill in first], [skill.name for skill in second])
        self.assertLessEqual(len(first), MAX_ACTIVE_SKILLS)
        self.assertNotIn("cloud-deploy", {skill.name for skill in first})
        self.assertEqual(select_skills("Explain ocean tides", skills), [])


if __name__ == "__main__":
    unittest.main()
