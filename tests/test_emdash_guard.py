"""Deterministic test suite for emdash-guard.

Run from the repository root:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EM_DASH = "\u2014"
EN_DASH = "\u2013"
HORIZONTAL_BAR = "\u2015"


def _load(name: str, path: Path, *, package: bool = False):
    kwargs = {"submodule_search_locations": [str(path.parent)]} if package else {}
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dashes = _load("dashes_under_test", ROOT / "dashes.py")
guard = _load("emdash_guard_under_test", ROOT / "__init__.py", package=True)


class HermesHomeTestCase(unittest.TestCase):
    """Every test runs against a throwaway HERMES_HOME: no test writes to a real profile."""

    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="emdash-guard-test-"))
        self._previous_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)
        # The guard module is imported once per process: reset its in-memory counters so
        # every test observes only what it triggered itself.
        guard._STATS = {"turns": 0, "dashes": 0, "last": None, "since": None}
        guard._LAST_NOTIFY = float("-inf")

    def tearDown(self) -> None:
        if self._previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._previous_home
        os.environ.pop("HERMES_EMDASH_NOTIFY", None)
        os.environ.pop("HERMES_EMDASH_ANNOUNCE", None)
        shutil.rmtree(self.home, ignore_errors=True)

    def assertFixed(self, text: str, expected: str, count: int) -> None:
        fixed, found = dashes.fix_dashes(text)
        self.assertEqual(fixed, expected)
        self.assertEqual(found, count)


class RewriteRules(HermesHomeTestCase):
    def test_spaced_em_dash_becomes_spaced_hyphen(self):
        self.assertFixed(f"Bonjour {EM_DASH} test", "Bonjour - test", 1)

    def test_unspaced_em_dash_gets_spaces(self):
        self.assertFixed(f"mot{EM_DASH}mot", "mot - mot", 1)

    def test_en_dash_and_horizontal_bar(self):
        self.assertFixed(f"a {EN_DASH} b", "a - b", 1)
        self.assertFixed(f"a {HORIZONTAL_BAR} b", "a - b", 1)

    def test_numeric_range_stays_glued(self):
        self.assertFixed(f"2018{EN_DASH}2020", "2018-2020", 1)
        self.assertFixed(f"01{EM_DASH}02", "01-02", 1)

    def test_line_final_dash_leaves_no_trailing_space(self):
        self.assertFixed(f"fin {EM_DASH}\nsuite", "fin -\nsuite", 1)

    def test_line_start_dash_keeps_markdown_bullet_shape(self):
        self.assertFixed(f"{EM_DASH} item", " - item", 1)

    def test_markdown_table_row(self):
        self.assertFixed(f"| a | b {EM_DASH} c |", "| a | b - c |", 1)

    def test_count_reports_every_occurrence(self):
        self.assertFixed(f"a {EM_DASH} b {EN_DASH} c", "a - b - c", 2)

    def test_clean_text_is_untouched(self):
        self.assertFixed("texte - normal\n2018-2020", "texte - normal\n2018-2020", 0)

    def test_non_string_and_empty_input(self):
        self.assertEqual(dashes.fix_dashes(""), ("", 0))
        self.assertEqual(dashes.fix_dashes(None), (None, 0))

    def test_accents_survive(self):
        self.assertFixed(f"Résumé {EM_DASH} testé", "Résumé - testé", 1)

    def test_idempotent_and_leaves_no_dash(self):
        for sample in (f"a {EM_DASH} b", f"2018{EN_DASH}2020 et {EM_DASH} suite",
                       f"{EM_DASH}{EM_DASH} double", f"Mot{EM_DASH}\n{EN_DASH} puce"):
            with self.subTest(sample=sample):
                once, _ = dashes.fix_dashes(sample)
                twice, count = dashes.fix_dashes(once)
                self.assertEqual(once, twice)
                self.assertEqual(count, 0)
                self.assertFalse(dashes.has_dash(once))


class FileScanning(HermesHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tree = self.home / "tree"
        self.tree.mkdir()
        self.dirty = self.tree / "cours.md"
        self.dirty.write_text(f"# Cours {EM_DASH} U5\nBloc 2018{EN_DASH}2020\nclean line\n", encoding="utf-8")
        self.clean = self.tree / "propre.md"
        self.clean.write_text("Aucun tiret cadratin ici.\n", encoding="utf-8")
        self.binary = self.tree / "image.png"
        self.binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00" + f"x{EM_DASH}y".encode())

    def test_scan_is_read_only_and_ignores_binaries(self):
        report = dashes.scan_targets([self.tree])
        self.assertEqual(report["files_scanned"], 2)
        self.assertEqual(report["files_hit"], 1)
        self.assertEqual(report["dashes"], 2)
        self.assertTrue(dashes.has_dash(self.dirty.read_text(encoding="utf-8")))

    def test_fix_rewrites_only_what_has_dashes(self):
        dashes.scan_targets([self.tree], fix=True)
        self.assertEqual(self.dirty.read_text(encoding="utf-8"),
                         "# Cours - U5\nBloc 2018-2020\nclean line\n")
        self.assertEqual(self.clean.read_text(encoding="utf-8"), "Aucun tiret cadratin ici.\n")
        self.assertTrue(self.binary.read_bytes().endswith(f"x{EM_DASH}y".encode()))

    def test_second_pass_finds_nothing(self):
        dashes.scan_targets([self.tree], fix=True)
        self.assertEqual(dashes.scan_targets([self.tree], fix=True)["dashes"], 0)

    def test_own_state_files_are_skipped(self):
        state = self.home / "state" / "emdash-guard.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(f'{{"sample": "a {EM_DASH} b"}}', encoding="utf-8")
        self.assertEqual(dashes.scan_targets([state.parent])["files_hit"], 0)


class PluginWiring(HermesHomeTestCase):
    class _Ctx:
        def __init__(self):
            self.hooks, self.commands = [], []

        def register_hook(self, name, callback):
            self.hooks.append(name)

        def register_command(self, name, callback, description="", args_hint=""):
            self.commands.append(name)
            self.callback = callback

    def test_register_wires_hook_and_command(self):
        ctx = self._Ctx()
        guard.register(ctx)
        self.assertEqual(ctx.hooks, ["transform_llm_output"])
        self.assertEqual(ctx.commands, ["dashes"])

    def test_transform_returns_none_on_clean_text(self):
        self.assertIsNone(guard.transform_llm_output(response_text="Rien a corriger - ici"))

    def test_transform_rewrites_logs_and_counters(self):
        fixed = guard.transform_llm_output(
            response_text=f"Bonjour{EM_DASH}voici.\nPlage 2018{EN_DASH}2020.\nligne propre",
            session_id="s", model="m", platform="desktop")
        self.assertEqual(fixed, "Bonjour - voici.\nPlage 2018-2020.\nligne propre")
        log_lines = (self.home / "logs" / "emdash-guard.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(log_lines), 1)
        entry = json.loads(log_lines[0])
        self.assertEqual((entry["count"], entry["platform"], entry["session_id"]), (2, "desktop", "s"))
        self.assertIn(EM_DASH, entry["sample"])  # the original is kept as evidence
        state = json.loads((self.home / "state" / "emdash-guard.json").read_text(encoding="utf-8"))
        self.assertEqual((state["dashes"], state["turns"]), (2, 1))

    def test_transform_is_idempotent_per_turn(self):
        once = guard.transform_llm_output(response_text=f"Test {EM_DASH} une fois")
        self.assertIsNone(guard.transform_llm_output(response_text=once))

    def test_status_command_reports_counters_and_notify_state(self):
        guard.transform_llm_output(response_text=f"Test {EM_DASH} statut")
        status = guard._command("status")
        self.assertIn("1 tiret(s) cadratin(s)", status)
        self.assertIn("Notification macOS : desactivee", status)

    def test_unknown_argument_is_answered_not_raised(self):
        self.assertIn("Argument inconnu", guard._command("bogus"))


class HomeResolution(HermesHomeTestCase):
    """The journal must follow the plugin copy, not the process environment: a multiplexed
    process serves every profile with the LAUNCH home in its environment."""

    def test_profile_copy_uses_the_home_that_hosts_it(self):
        pkg = self.home / "plugins" / "emdash-guard"
        pkg.mkdir(parents=True)
        for name in ("__init__.py", "dashes.py"):
            shutil.copy2(ROOT / name, pkg / name)
        os.environ["HERMES_HOME"] = str(self.home / "somewhere-else")  # launcher home wins for process env
        loaded = _load("emdash_guard_profile_copy", pkg / "__init__.py", package=True)
        self.assertEqual(loaded._home().resolve(), self.home.resolve())

    def test_standalone_copy_falls_back_to_hermes_home(self):
        self.assertEqual(guard._home().resolve(), self.home.resolve())


class NotificationDefault(HermesHomeTestCase):
    def test_notification_is_off_unless_opted_in(self):
        with mock.patch.object(guard.subprocess, "Popen") as popen:
            guard.transform_llm_output(response_text=f"Test {EM_DASH} sans notif")
            popen.assert_not_called()

    def test_status_command_shows_notification_off(self):
        self.assertIn("desactivee", guard._command("status"))

    def test_notify_enabled_follows_the_env_var(self):
        os.environ["HERMES_EMDASH_NOTIFY"] = "1"
        self.assertEqual(guard._notify_enabled(), sys.platform == "darwin")
        os.environ["HERMES_EMDASH_NOTIFY"] = "0"
        self.assertFalse(guard._notify_enabled())

    @unittest.skipUnless(sys.platform == "darwin", "macOS notification is darwin-only")
    def test_opt_in_fires_one_notification_per_minute(self):
        os.environ["HERMES_EMDASH_NOTIFY"] = "1"
        guard._LAST_NOTIFY = float("-inf")
        with mock.patch.object(guard.subprocess, "Popen") as popen:
            guard.transform_llm_output(response_text=f"Test {EM_DASH} opt-in")
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(popen.call_args.args[0][0], "osascript")
            guard.transform_llm_output(response_text=f"Test {EM_DASH} encore")
            self.assertEqual(popen.call_count, 1)  # rate limited


class AnnounceMode(HermesHomeTestCase):
    def test_announce_is_off_by_default(self):
        out = guard.transform_llm_output(response_text=f"Test {EM_DASH} marqueur")
        self.assertNotIn("emdash-guard", out)

    def test_announce_appends_a_visible_marker(self):
        os.environ["HERMES_EMDASH_ANNOUNCE"] = "1"
        out = guard.transform_llm_output(response_text=f"Test {EM_DASH} marqueur")
        self.assertIn("emdash-guard", out)
        self.assertNotIn(EM_DASH, out)


class CliContract(HermesHomeTestCase):
    def _run(self, *args, stdin: str = ""):
        return subprocess.run([sys.executable, str(ROOT / "dashes.py"), *args],
                              input=stdin, capture_output=True, text=True)

    def test_stdin_filter(self):
        proc = self._run("--stdin", stdin=f"ligne {EM_DASH} test")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "ligne - test")

    def test_check_exits_nonzero_when_a_dash_remains(self):
        target = self.home / "doc.md"
        target.write_text(f"texte {EM_DASH} sale\n", encoding="utf-8")
        self.assertEqual(self._run("--check", str(target)).returncode, 1)
        self.assertEqual(self._run("--fix", str(target)).returncode, 0)
        self.assertEqual(self._run("--check", str(target)).returncode, 0)

    def test_unknown_path_is_reported_not_crashed(self):
        proc = self._run("--check", str(self.home / "absent.md"))
        self.assertEqual(proc.returncode, 0)
        self.assertIn("introuvable", proc.stderr)


if __name__ == "__main__":
    unittest.main()
