from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scene_catalog import (
    initialize_scene_catalog,
    link_iq_recording,
    list_linked_iq_details,
    resolve_linked_iq_detail,
)


class ResolveLinkedIQDetailTests(unittest.TestCase):
    def test_relocates_stale_paths_under_current_scene_iq_root(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            database = temporary / "catalog.db"
            iq_root = temporary / "current-iq-root"
            data_directory = iq_root / "IQ-DATA-4"
            data_directory.mkdir(parents=True)
            stem = "longx823m"
            current_paths = tuple(data_directory / f"{stem}{suffix}" for suffix in (".wsm", ".ws1", ".ws2"))
            for path in current_paths:
                path.touch()

            initialize_scene_catalog(database, (("北京", "测试点"),))
            link_iq_recording(
                database,
                "北京",
                "测试点",
                stem,
                f"F:/old-root/{stem}.wsm",
                f"F:/old-root/{stem}.ws1",
                f"F:/old-root/{stem}.ws2",
            )

            resolved = resolve_linked_iq_detail(
                database, "北京", "测试点", stem, iq_root
            )

            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertTrue(Path(resolved.wsm_file).samefile(current_paths[0]))
            self.assertTrue(Path(resolved.ws1_file).samefile(current_paths[1]))
            self.assertTrue(Path(resolved.ws2_file).samefile(current_paths[2]))
            self.assertEqual(list_linked_iq_details(database, "北京", "测试点"), [resolved])

    def test_returns_existing_scene_link_without_relocation(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            database = temporary / "catalog.db"
            stem = "sample385m"
            paths = tuple(temporary / f"{stem}{suffix}" for suffix in (".wsm", ".ws1", ".ws2"))
            for path in paths:
                path.touch()

            initialize_scene_catalog(database, (("北京", "测试点"),))
            link_iq_recording(database, "北京", "测试点", stem, *(str(path) for path in paths))

            resolved = resolve_linked_iq_detail(database, "北京", "测试点", stem)

            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(Path(resolved.wsm_file), paths[0])


if __name__ == "__main__":
    unittest.main()
