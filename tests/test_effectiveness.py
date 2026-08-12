from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "plugins" / "codex-gardener" / "scripts" / "effectiveness.py"
SPEC = importlib.util.spec_from_file_location("effectiveness_under_test", SCRIPT)
assert SPEC and SPEC.loader
effectiveness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(effectiveness)


class EffectivenessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "plugin-data"
        self.env = patch.dict(
            os.environ,
            {
                "CODEX_GARDENER_DATA": str(self.root),
                "CODEX_HOME": str(Path(self.temp.name) / "codex-home"),
                effectiveness.OPT_OUT_ENV: "1",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.session = effectiveness.hash_identifier("raw-session", "session")
        self.project = effectiveness.hash_identifier("C:/private/repository", "project")
        assert self.session and self.project

    def records(self) -> list[dict]:
        values, corrupt = effectiveness.read_events(root=self.root)
        self.assertEqual(corrupt, 0)
        return values

    def test_allowlist_excludes_sensitive_payloads_and_raw_identifiers(self) -> None:
        secret = "top-secret prompt and tool output"
        self.assertTrue(
            effectiveness.log_event(
                "session_start",
                root=self.root,
                session="raw-session",
                project="C:/private/repository",
                prompt=secret,
                tool_input={"path": "C:/private/repository"},
                transcript=secret,
                raw_session_id="raw-session",
            )
        )
        rendered = effectiveness.log_path(self.root).read_text(encoding="utf-8")
        self.assertNotIn(secret, rendered)
        self.assertNotIn("C:/private/repository", rendered)
        self.assertNotIn("raw-session", rendered)
        self.assertEqual(set(self.records()[0]), {"schema_version", "event", "created_at", "session", "project"})

    def test_opt_out_writes_nothing(self) -> None:
        with patch.dict(os.environ, {effectiveness.OPT_OUT_ENV: "0"}):
            self.assertFalse(
                effectiveness.log_event("session_start", root=self.root, session=self.session, project=self.project)
            )
        self.assertFalse(effectiveness.log_path(self.root).exists())

    def test_logging_failure_fails_open(self) -> None:
        with patch.object(effectiveness, "file_lock", side_effect=OSError("simulated storage failure")):
            self.assertFalse(
                effectiveness.log_event("session_start", root=self.root, session=self.session, project=self.project)
            )

    def test_concurrent_writes_remain_valid(self) -> None:
        def write(_: int) -> bool:
            return effectiveness.log_event(
                "context_lookup",
                root=self.root,
                session=self.session,
                project=self.project,
                repository_available=2,
                global_available=1,
                repository_hits=1,
                global_hits=0,
                injected=1,
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertTrue(all(pool.map(write, range(80))))
        self.assertEqual(len(self.records()), 80)

    def test_rotation_bounds_files_and_retention(self) -> None:
        with patch.object(effectiveness, "MAX_LOG_BYTES", 180), patch.object(effectiveness, "MAX_BACKUPS", 2):
            for _ in range(20):
                effectiveness.log_event(
                    "session_start", root=self.root, session=self.session, project=self.project
                )
            paths = effectiveness.event_paths(self.root)
            self.assertLessEqual(len(paths), 3)
            self.assertTrue(effectiveness.log_path(self.root).is_file())

            retention_root = Path(self.temp.name) / "retention-data"
            effectiveness.log_event(
                "session_start", root=retention_root, session=self.session, project=self.project
            )
            active = effectiveness.log_path(retention_root)
            old = datetime.now(timezone.utc).timestamp() - (effectiveness.RETENTION_DAYS + 2) * 86400
            os.utime(active, (old, old))
            effectiveness.log_event(
                "session_start", root=retention_root, session=self.session, project=self.project
            )
            self.assertEqual(len(effectiveness.event_paths(retention_root)), 1)
            stale_backup = active.with_name("events.1.jsonl")
            stale_backup.write_text("{}\n", encoding="utf-8")
            os.utime(stale_backup, (old, old))
            effectiveness.log_event(
                "session_start", root=retention_root, session=self.session, project=self.project
            )
            if stale_backup.exists():
                self.assertNotEqual(stale_backup.read_text(encoding="utf-8"), "{}\n")
                self.assertGreater(stale_backup.stat().st_mtime, old)

    def test_summary_filters_dates_and_ignores_corrupt_lines(self) -> None:
        now = datetime(2026, 8, 12, tzinfo=timezone.utc)
        path = effectiveness.log_path(self.root)
        path.parent.mkdir(parents=True)
        recent = effectiveness.build_event("session_start", session=self.session, project=self.project)
        old = effectiveness.build_event("session_start", session=self.session, project=self.project)
        assert recent and old
        recent["created_at"] = (now - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        old["created_at"] = (now - timedelta(days=30)).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps(old) + "\ntruncated {\n" + json.dumps(recent) + "\n", encoding="utf-8")
        report = effectiveness.summarize(since_days=14, root=self.root, now=now)
        self.assertEqual(report["events"]["valid"], 1)
        self.assertEqual(report["events"]["corrupt_lines_ignored"], 1)

    def test_summary_exposes_learning_denominators_and_distributions(self) -> None:
        common = {"root": self.root, "session": self.session, "project": self.project}
        effectiveness.log_event("session_start", **common)
        effectiveness.log_event("review_requested", **common, signals=["workspace_changed"])
        effectiveness.log_event(
            "context_lookup",
            **common,
            repository_available=3,
            global_available=2,
            repository_hits=1,
            global_hits=1,
            injected=2,
        )
        effectiveness.log_event(
            "context_lookup",
            **common,
            repository_available=3,
            global_available=2,
            repository_hits=0,
            global_hits=0,
            injected=0,
        )
        effectiveness.log_event(
            "capture_recorded",
            **common,
            knowledge_scope="global",
            recommended_target="skill",
            confidence_bucket="high",
        )
        effectiveness.log_event(
            "resolution_recorded",
            root=self.root,
            project=self.project,
            knowledge_scope="global",
            status="promoted",
            target="skill",
        )
        effectiveness.log_event("pending_queued", **common, signals=["workspace_changed"])
        effectiveness.log_event("pending_resolved", **common)
        effectiveness.log_event(
            "project_boundary_denied",
            root=self.root,
            session=self.session,
            primary_project=self.project,
            target_project=effectiveness.hash_identifier("other", "project"),
            tool_category="patch",
        )
        report = effectiveness.summarize(root=self.root)
        self.assertEqual(report["context"]["lookups"], 2)
        self.assertEqual(report["context"]["lookups_with_hits"], 1)
        self.assertEqual(report["context"]["lookup_hit_rate"], 0.5)
        self.assertEqual(report["context"]["hits_repository"], 1)
        self.assertEqual(report["context"]["hits_global"], 1)
        self.assertEqual(report["candidates"]["knowledge_scope"], {"global": 1})
        self.assertEqual(report["reviews"]["candidates_per_requested_review"], 1.0)
        self.assertEqual(report["candidates"]["recommended_target"], {"skill": 1})
        self.assertEqual(report["resolutions"]["status"], {"promoted": 1})
        self.assertEqual(report["reviews"]["pending_queued"], 1)
        self.assertEqual(report["reviews"]["pending_resolved"], 1)
        self.assertEqual(report["boundary"]["denials"], 1)


if __name__ == "__main__":
    unittest.main()
