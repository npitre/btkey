#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Installing it, and the repository staying runnable afterwards.

Two ways to run btkey and they must not be the same one twice.  A launcher
installed to /usr/local/bin has the package beside it at a path baked in;
the one in the checkout finds the package next to itself.  Get that wrong
and the installed copy silently runs whatever is in somebody's working
tree, or does not run at all - and the failure only shows up on a machine
that has no checkout.

These tests do a real install into a temporary prefix and use it.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import btkey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCHERS = ("btkey", "btkey-trace-input", "btkey-system-bluetoothd")


@unittest.skipIf(shutil.which("make") is None, "make is not installed")
class InstallTest(unittest.TestCase):
    def setUp(self):
        self.prefix = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.prefix, ignore_errors=True)
        self.make("install")

    def make(self, target, **extra):
        arguments = ["make", "-C", ROOT, target, "PREFIX=" + self.prefix]
        arguments += ["%s=%s" % pair for pair in extra.items()]
        result = subprocess.run(arguments, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0,
                         result.stdout + result.stderr)
        return result

    def path(self, *parts):
        return os.path.join(self.prefix, *parts)

    def run_installed(self, *arguments):
        return subprocess.run([self.path("bin", "btkey")] + list(arguments),
                              capture_output=True, text=True)

    # -- what lands where ------------------------------------------------

    def test_every_module_is_installed(self):
        # A module added to the package and not to the install is missing
        # only on machines without a checkout, which is every other one.
        installed = set(os.listdir(self.path("lib", "btkey", "btkey")))
        source = {name for name in os.listdir(os.path.join(ROOT, "btkey"))
                  if name.endswith(".py")}
        self.assertEqual(source - installed, set())

    def test_the_launchers_are_installed_and_executable(self):
        for name in LAUNCHERS:
            target = self.path("bin", name)
            self.assertTrue(os.path.exists(target), target)
            self.assertTrue(os.access(target, os.X_OK), target)

    def test_the_examples_and_a_worked_layout_come_too(self):
        self.assertTrue(os.path.exists(
            self.path("share", "btkey", "examples", "btkey.conf.example")))
        self.assertTrue(os.listdir(self.path("share", "btkey", "layouts")))

    def test_the_documentation_comes_too(self):
        self.assertTrue(os.path.exists(
            self.path("share", "doc", "btkey", "README.md")))

    # -- and whether it runs ---------------------------------------------

    def test_the_installed_launcher_runs(self):
        result = self.run_installed("--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(btkey.__version__, result.stdout)

    def test_it_runs_from_anywhere(self):
        # sys.path[0] is the launcher's own directory, which is not where
        # the package is.
        result = subprocess.run([self.path("bin", "btkey"), "--version"],
                                capture_output=True, text=True, cwd="/")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_it_uses_the_installed_package_not_the_checkout(self):
        # Proved by changing the installed copy and watching the answer
        # change.  If it were falling back to the checkout, it would not.
        target = self.path("lib", "btkey", "btkey", "__init__.py")
        with open(target, encoding="utf-8") as handle:
            original = handle.read()
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(original.replace(btkey.__version__, "9.9.9-installed"))
        result = self.run_installed("--version")
        self.assertIn("9.9.9-installed", result.stdout)

    @staticmethod
    def helptext(launcher):
        """--help output with the wrapping taken out.

        argparse folds the help to the terminal width, so a phrase to look
        for can arrive with a newline through the middle of it.
        """
        result = subprocess.run([launcher, "--help"], capture_output=True,
                                text=True, cwd="/")
        return " ".join(result.stdout.split())

    def test_an_installed_btkey_writes_no_log_by_default(self):
        """The log is a developer's tool, and a keystroke history besides.

        It holds every message btkey printed, a displayed pairing passkey
        among them.  Worth having while working on btkey; not worth writing
        on a machine that is only using it.
        """
        self.assertIn("it is off, since this is an installed btkey",
                      self.helptext(self.path("bin", "btkey")))

    def test_a_checkout_does_write_one(self):
        self.assertIn("since this is a source checkout",
                      self.helptext(os.path.join(ROOT, "bin", "btkey")))

    def parsed(self, path, expression):
        """Evaluate something against the package at `path`."""
        script = ("import sys; sys.path.insert(0, %r); "
                  "import btkey; from btkey import cli; print(%s)"
                  % (path, expression))
        result = subprocess.run([sys.executable, "-c", script],
                                capture_output=True, text=True, cwd="/")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_the_installed_default_really_is_no_log(self):
        # The help says so; this is the value the help is describing.
        self.assertEqual(
            self.parsed(self.path("lib", "btkey"),
                        "repr(cli.build_parser().parse_args([]).log_file)"),
            "''")

    def test_the_checkout_default_really_is_a_log(self):
        self.assertEqual(
            self.parsed(ROOT,
                        "cli.build_parser().parse_args([]).log_file"),
            "/run/btkey/log")

    def test_the_installed_package_knows_it_is_installed(self):
        # Worked out from the layout, so a tree copied elsewhere is still
        # recognised for what it is and nothing has to be stamped in.
        script = ("import sys; sys.path.insert(0, %r); import btkey; "
                  "print(btkey.from_checkout())"
                  % self.path("lib", "btkey"))
        result = subprocess.run([sys.executable, "-c", script],
                                capture_output=True, text=True, cwd="/")
        self.assertEqual(result.stdout.strip(), "False", result.stderr)

    def test_the_checkout_still_uses_the_checkout(self):
        result = subprocess.run([os.path.join(ROOT, "bin", "btkey"),
                                 "--version"], capture_output=True, text=True)
        self.assertIn(btkey.__version__, result.stdout)

    # -- and undoing it --------------------------------------------------

    def test_it_is_byte_compiled_at_install_time(self):
        # Or the first run, which is as root, scatters a root-owned
        # __pycache__ through the install directory.
        cached = os.path.join(self.path("lib", "btkey", "btkey"), "__pycache__")
        self.assertTrue(os.path.isdir(cached))
        self.assertTrue(os.listdir(cached))

    def test_uninstall_leaves_nothing_behind(self):
        self.make("uninstall")
        left = [os.path.join(base, name)
                for base, _, names in os.walk(self.prefix) for name in names]
        self.assertEqual(left, [])


@unittest.skipIf(shutil.which("make") is None, "make is not installed")
class UserInstallTest(unittest.TestCase):
    """Installing without root, which is worth having even though btkey
    needs root to run: --learn-layout, --cancel and --build-layout are all
    run by the person at the keyboard.

    HOME is pointed somewhere disposable, so these never write to a real
    one.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def install(self, *extra):
        environment = dict(os.environ, HOME=self.home)
        result = subprocess.run(["make", "-C", ROOT, "install"] + list(extra),
                                capture_output=True, text=True,
                                env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_the_default_prefix_is_under_home_when_not_root(self):
        # Rather than /usr/local, which a user cannot write to, so that
        # plain `make install` does something useful instead of failing.
        self.install()
        self.assertTrue(os.path.exists(
            os.path.join(self.home, ".local/bin/btkey")))

    def test_it_runs_from_there(self):
        self.install()
        result = subprocess.run(
            [os.path.join(self.home, ".local/bin/btkey"), "--version"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(btkey.__version__, result.stdout)

    def test_prefix_home_puts_it_in_bin(self):
        self.install("PREFIX=" + self.home)
        launcher = os.path.join(self.home, "bin/btkey")
        self.assertTrue(os.path.exists(launcher))
        result = subprocess.run([launcher, "--version"],
                                capture_output=True, text=True)
        self.assertIn(btkey.__version__, result.stdout)

    def test_bindir_alone_moves_only_the_launchers(self):
        target = os.path.join(self.home, "bin")
        self.install("BINDIR=" + target)
        self.assertTrue(os.path.exists(os.path.join(target, "btkey")))
        # The package stayed where the prefix put it, and is still found.
        self.assertTrue(os.path.exists(
            os.path.join(self.home, ".local/lib/btkey/btkey/cli.py")))
        result = subprocess.run([os.path.join(target, "btkey"), "--version"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_it_says_that_running_it_still_needs_root(self):
        # The trap in a user install: sudo will never look in ~/bin, so
        # `sudo btkey` fails with nothing but "command not found".
        said = self.install().stdout
        self.assertIn("needs root", said)
        self.assertIn("sudo " + os.path.join(self.home, ".local/bin/btkey"),
                      said)

    def test_it_says_when_the_launcher_will_not_be_on_the_path(self):
        # A launcher nobody can invoke by name is the whole point missed,
        # and nothing else would have said so.
        environment = dict(os.environ, HOME=self.home, PATH="/usr/bin:/bin")
        result = subprocess.run(["make", "-C", ROOT, "install"],
                                capture_output=True, text=True,
                                env=environment)
        self.assertIn("not on your PATH", result.stdout)

    def test_it_keeps_quiet_when_the_path_is_already_right(self):
        target = os.path.join(self.home, ".local/bin")
        environment = dict(os.environ, HOME=self.home,
                           PATH=target + ":/usr/bin:/bin")
        result = subprocess.run(["make", "-C", ROOT, "install"],
                                capture_output=True, text=True,
                                env=environment)
        self.assertNotIn("not on your PATH", result.stdout)

    def test_uninstall_finds_the_same_place_the_install_used(self):
        self.install()
        environment = dict(os.environ, HOME=self.home)
        subprocess.run(["make", "-C", ROOT, "uninstall"],
                       capture_output=True, text=True, env=environment)
        self.assertFalse(os.path.exists(
            os.path.join(self.home, ".local/bin/btkey")))


@unittest.skipIf(shutil.which("make") is None, "make is not installed")
class StagedInstallTest(unittest.TestCase):
    """DESTDIR, for building a package rather than installing one."""

    def test_the_tree_is_staged_under_destdir(self):
        stage = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, stage, ignore_errors=True)
        result = subprocess.run(
            ["make", "-C", ROOT, "install", "PREFIX=/usr", "DESTDIR=" + stage],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(os.path.join(stage, "usr/bin/btkey")))

    def test_the_staging_path_does_not_leak_into_the_bytecode(self):
        # A .pyc records where its source was, and with DESTDIR that is a
        # build directory nobody will ever have.
        stage = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, stage, ignore_errors=True)
        subprocess.run(
            ["make", "-C", ROOT, "install", "PREFIX=/usr", "DESTDIR=" + stage],
            capture_output=True, text=True)
        cached = os.path.join(stage, "usr/lib/btkey/btkey/__pycache__")
        found = [name for name in os.listdir(cached) if name.endswith(".pyc")]
        self.assertTrue(found, "nothing was byte-compiled")
        for name in found:
            with open(os.path.join(cached, name), "rb") as handle:
                self.assertNotIn(stage.encode(), handle.read(), name)

    def test_a_staged_launcher_points_at_the_real_prefix(self):
        # Not at the staging directory, which will not exist on the
        # machine the package is installed on.
        stage = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, stage, ignore_errors=True)
        subprocess.run(
            ["make", "-C", ROOT, "install", "PREFIX=/usr", "DESTDIR=" + stage],
            capture_output=True, text=True)
        with open(os.path.join(stage, "usr/bin/btkey"), encoding="utf-8") as f:
            launcher = f.read()
        self.assertIn('LIBDIR = "/usr/lib/btkey"', launcher)
        self.assertNotIn(stage, launcher)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
