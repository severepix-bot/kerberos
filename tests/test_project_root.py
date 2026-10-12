import tempfile
import unittest
from pathlib import Path

from assistente.project_root import resolve_project_root


class ProjectRootTests(unittest.TestCase):
    def test_defaults_to_the_assistant_project_when_override_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            default_root = Path(directory)

            result = resolve_project_root(default_root, environ={})

        self.assertEqual(result, default_root.resolve())

    def test_uses_a_configured_project_root_and_resolves_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory) / "project"
            project_root.mkdir()
            (project_root / "package.json").write_text("{}", encoding="utf-8")
            alias = Path(directory) / "project-alias"
            alias.symlink_to(project_root, target_is_directory=True)

            result = resolve_project_root(
                Path(__file__).resolve().parent.parent,
                environ={"CEREBRO_PROJECT_ROOT": str(alias)},
                home=Path(directory) / "home",
            )

        self.assertEqual(result, project_root.resolve())

    def test_rejects_empty_relative_missing_and_non_directory_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file_path = root / "not-a-directory"
            file_path.write_text("file", encoding="utf-8")

            invalid_values = ("", "relative/project", str(root / "missing"), str(file_path))
            for value in invalid_values:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    resolve_project_root(root, environ={"CEREBRO_PROJECT_ROOT": value})

    def test_rejects_broad_or_unmarked_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            broad_directory = root / "broad"
            broad_directory.mkdir()
            project_root = root / "project"
            project_root.mkdir()
            (project_root / "src").mkdir()

            for path in (Path("/"), home, broad_directory, project_root):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    resolve_project_root(
                        root,
                        environ={"CEREBRO_PROJECT_ROOT": str(path)},
                        home=home,
                    )


if __name__ == "__main__":
    unittest.main()
