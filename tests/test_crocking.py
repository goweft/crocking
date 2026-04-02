#!/usr/bin/env python3
"""Tests for crocking — AI authorship detector."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from crocking.core import (
    CommitAnalyzer, ScanReport, AuthorProfile, Signal, Confidence,
    format_report, get_commits, check_file_markers, main, __version__,
)


def _git(repo, *args, env_extra=None):
    """Helper to run git commands in a temp repo."""
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "Test Author"
    env["GIT_AUTHOR_EMAIL"] = "test@example.com"
    env["GIT_COMMITTER_NAME"] = "Test Author"
    env["GIT_COMMITTER_EMAIL"] = "test@example.com"
    if env_extra:
        env.update(env_extra)
    subprocess.run(
        ["git", "-C", repo] + list(args),
        capture_output=True, text=True, env=env, check=True,
    )


def make_repo(path):
    """Initialize a git repo with initial commit."""
    subprocess.run(["git", "init", "-b", "main", path],
                   capture_output=True, check=True)
    _git(path, "config", "user.name", "Test Author")
    _git(path, "config", "user.email", "test@example.com")
    (Path(path) / "README.md").write_text("# test\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial commit")


class TestConfidence(unittest.TestCase):
    def test_weights(self):
        self.assertEqual(Confidence.DEFINITIVE.weight, 1.0)
        self.assertEqual(Confidence.HIGH.weight, 0.7)
        self.assertEqual(Confidence.LOW.weight, 0.15)

    def test_colors(self):
        self.assertIn("\033[", Confidence.DEFINITIVE.color)


class TestSignal(unittest.TestCase):
    def test_to_dict(self):
        s = Signal(rule_id="AUTH-001", confidence=Confidence.HIGH,
                   commit_hash="abc123def456", message="test signal")
        d = s.to_dict()
        self.assertEqual(d["rule_id"], "AUTH-001")
        self.assertEqual(d["commit_hash"], "abc123de")
        self.assertEqual(d["confidence"], "HIGH")


class TestKnownMarkers(unittest.TestCase):
    def test_copilot_trailer(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            (Path(d) / "app.py").write_text("print('hello')\n")
            _git(d, "add", ".")
            _git(d, "commit", "-m",
                 "feat: add app\n\nCo-authored-by: copilot-ai <copilot@github.com>")
            analyzer = CommitAnalyzer(d, max_commits=10)
            report = analyzer.scan()
            self.assertIn("AUTH-001", [s.rule_id for s in report.signals])

    def test_claude_trailer(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            (Path(d) / "main.py").write_text("x = 1\n")
            _git(d, "add", ".")
            _git(d, "commit", "-m",
                 "refactor: clean up\n\nCo-authored-by: Claude <claude@anthropic.com>")
            analyzer = CommitAnalyzer(d, max_commits=10)
            report = analyzer.scan()
            self.assertIn("AUTH-001", [s.rule_id for s in report.signals])

    def test_clean_commit_no_markers(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            (Path(d) / "util.py").write_text("def add(a, b): return a + b\n")
            _git(d, "add", ".")
            _git(d, "commit", "-m", "add utility function")
            analyzer = CommitAnalyzer(d, max_commits=10)
            report = analyzer.scan()
            self.assertEqual(len([s for s in report.signals if s.rule_id == "AUTH-001"]), 0)


class TestFileMarkers(unittest.TestCase):
    def test_claude_directory(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            (Path(d) / ".claude").mkdir()
            (Path(d) / ".claude" / "settings.json").write_text("{}")
            self.assertIn(".claude/", [m["marker"] for m in check_file_markers(d)])

    def test_cursor_directory(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            (Path(d) / ".cursor").mkdir()
            self.assertIn(".cursor/", [m["marker"] for m in check_file_markers(d)])

    def test_claude_md(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            (Path(d) / "CLAUDE.md").write_text("# project\n")
            self.assertIn("CLAUDE.md", [m["marker"] for m in check_file_markers(d)])

    def test_no_markers(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            self.assertEqual(len(check_file_markers(d)), 0)


class TestDiffStructure(unittest.TestCase):
    def test_bulk_file_creation(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            for i in range(6):
                (Path(d) / f"module_{i}.py").write_text(
                    f"# Module {i}\n" + "def func():\n    pass\n" * 30 + "\n")
            _git(d, "add", ".")
            _git(d, "commit", "-m", "feat: add all modules")
            analyzer = CommitAnalyzer(d, max_commits=10)
            report = analyzer.scan()
            self.assertTrue(len([s for s in report.signals if s.rule_id == "AUTH-004"]) > 0)

    def test_small_commit_no_signal(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            (Path(d) / "fix.py").write_text("x = 1\n")
            _git(d, "add", ".")
            _git(d, "commit", "-m", "fix: typo")
            analyzer = CommitAnalyzer(d, max_commits=10)
            report = analyzer.scan()
            self.assertEqual(len([s for s in report.signals if s.rule_id == "AUTH-004"]), 0)


class TestTimingPatterns(unittest.TestCase):
    def test_burst_commits(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            for i in range(5):
                ts = f"2026-04-01T10:00:{i:02d}+00:00"
                (Path(d) / f"file_{i}.txt").write_text(f"content {i}\n")
                _git(d, "add", ".",
                     env_extra={"GIT_AUTHOR_DATE": ts, "GIT_COMMITTER_DATE": ts})
                _git(d, "commit", "-m", f"update file {i}",
                     env_extra={"GIT_AUTHOR_DATE": ts, "GIT_COMMITTER_DATE": ts})
            analyzer = CommitAnalyzer(d, max_commits=20)
            report = analyzer.scan()
            self.assertTrue(len([s for s in report.signals if s.rule_id == "AUTH-003"]) > 0)


class TestMessageUniformity(unittest.TestCase):
    def test_uniform_conventional_commits(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            messages = [
                "feat(auth): implement user login flow with OAuth2 validation",
                "feat(auth): add password reset functionality with email verification",
                "feat(api): create RESTful endpoint for user profile management",
                "feat(api): implement rate limiting middleware for API endpoints",
                "fix(auth): resolve token refresh race condition in session handler",
                "fix(api): correct pagination offset calculation in list endpoint",
                "feat(db): add database migration for user preferences table schema",
                "feat(ui): implement responsive navigation component with dropdown",
                "fix(ui): resolve hydration mismatch in server-side rendering flow",
                "feat(api): add comprehensive input validation for form submissions",
            ]
            for i, msg in enumerate(messages):
                (Path(d) / f"src_{i}.py").write_text(f"# code {i}\n")
                _git(d, "add", ".")
                _git(d, "commit", "-m", msg)
            analyzer = CommitAnalyzer(d, max_commits=50)
            report = analyzer.scan()
            self.assertTrue(len([s for s in report.signals if s.rule_id == "AUTH-002"]) > 0)


class TestScanReport(unittest.TestCase):
    def test_scan_basic_repo(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            analyzer = CommitAnalyzer(d, max_commits=10)
            report = analyzer.scan()
            self.assertEqual(report.total_commits, 1)
            self.assertEqual(report.total_authors, 1)

    def test_scan_not_a_repo(self):
        with tempfile.TemporaryDirectory() as d:
            analyzer = CommitAnalyzer(d, max_commits=10)
            report = analyzer.scan()
            self.assertTrue(any(s.rule_id == "ERR-001" for s in report.signals))

    def test_to_dict(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            analyzer = CommitAnalyzer(d, max_commits=10)
            report = analyzer.scan()
            d_out = report.to_dict()
            self.assertEqual(d_out["version"], __version__)
            self.assertIn("total_commits", d_out)
            self.assertIn("authors", d_out)


class TestCheckCommit(unittest.TestCase):
    def test_check_single_commit(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            (Path(d) / "app.py").write_text("print('hi')\n")
            _git(d, "add", ".")
            _git(d, "commit", "-m",
                 "add app\n\nCo-authored-by: copilot-ai <copilot@github.com>")
            result = subprocess.run(
                ["git", "-C", d, "log", "-1", "--format=%H"],
                capture_output=True, text=True)
            commit_hash = result.stdout.strip()
            analyzer = CommitAnalyzer(d)
            signals = analyzer.check_commit(commit_hash)
            self.assertIn("AUTH-001", [s.rule_id for s in signals])


class TestFormatting(unittest.TestCase):
    def test_format_empty_report(self):
        report = ScanReport(repo_path="/tmp/test")
        output = format_report(report, use_color=False)
        self.assertIn("crocking scan report", output)
        self.assertIn("No AI authorship signals", output)

    def test_format_with_signals(self):
        report = ScanReport(repo_path="/tmp/test", total_commits=10, total_authors=1)
        report.authors.append(AuthorProfile(
            name="Bot", email="bot@ai.com", total_commits=5, ai_score=75.0,
            signals=[Signal(rule_id="AUTH-001", confidence=Confidence.DEFINITIVE,
                           commit_hash="abc123", message="test")]))
        output = format_report(report, use_color=False)
        self.assertIn("AUTH-001", output)
        self.assertIn("LIKELY AI", output)


class TestCLI(unittest.TestCase):
    def test_no_args(self):
        sys.argv = ["crocking"]
        self.assertEqual(main(), 2)

    def test_scan_current_dir(self):
        with tempfile.TemporaryDirectory() as d:
            make_repo(d)
            sys.argv = ["crocking", "scan", d, "--format", "json"]
            self.assertEqual(main(), 0)

    def test_scan_not_a_repo(self):
        with tempfile.TemporaryDirectory() as d:
            sys.argv = ["crocking", "scan", d, "--format", "json"]
            self.assertEqual(main(), 0)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    total = passed = failed = 0
    errors = []
    for group in suite:
        for test in group:
            total += 1
            name = str(test)
            try:
                test.debug()
                passed += 1
                print(f"  \033[92m\u2713\033[0m {name}")
            except Exception as e:
                failed += 1
                errors.append((name, e))
                print(f"  \033[91m\u2717\033[0m {name}")
                print(f"    {e}")
    print()
    print("=" * 60)
    print(f"  Total: {total}  Passed: {passed}  Failed: {failed}")
    print("=" * 60)
    if errors:
        for name, e in errors:
            print(f"  FAIL: {name}\n        {e}")
        sys.exit(1)
    sys.exit(0)
