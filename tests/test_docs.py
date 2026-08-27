# SPDX-License-Identifier: GPL-2.0-only
"""The documentation, checked against the interface it describes.

A documentation edit fails silently - a search and replace whose anchor
has moved simply does nothing - so nothing else catches prose that has
drifted away from the code.

Everything here is a rule about the current interface rather than a list
of things once said and since removed.  Such a list passes for good the
moment it is cleaned up, and then only grows.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from btkey import cli

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = ["README.md"] + [os.path.join("docs", name)
                        for name in sorted(os.listdir(os.path.join(ROOT, "docs")))
                        if name.endswith(".md")]
# The config example names options as well, without the dashes.
EXAMPLE = "examples/btkey.conf.example"

# Options belonging to other programs, which the documentation quotes when
# explaining what btkey does to them.
FOREIGN = {"--compat", "--noplugin", "--nodetach", "--configfile",
           "--undo", "--permanent"}

def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


class OptionsTest(unittest.TestCase):
    def setUp(self):
        parser = cli.build_parser()
        self.known = set()
        for action in parser._actions:                  # noqa: SLF001
            self.known.update(action.option_strings)

        # What a config file may say, which is not the same set: a switch
        # is named positively there, so --no-audio is written `audio`.
        # Checking against the option strings would accept `no-audio`,
        # which the parser rejects.
        self.config_keys = set(cli.flag_options(parser))
        for action in parser._actions:                  # noqa: SLF001
            if action.nargs == 0:
                continue
            for option in action.option_strings:
                if option.startswith("--"):
                    self.config_keys.add(option[2:])

    def test_every_option_mentioned_exists(self):
        for name in DOCS:
            mentioned = set(re.findall(r"`(--[a-z][a-z-]*)`", read(name)))
            for option in mentioned - FOREIGN:
                self.assertIn(option, self.known,
                              "%s names %s, which does not exist"
                              % (name, option))


    def test_every_option_in_the_config_example_exists(self):
        for line in read(EXAMPLE).splitlines():
            # Only settings, which are `key = value`.  Prose comments are
            # prose: an earlier version of this took "passkey." for an
            # option name.
            key, found, _ = line.lstrip("#").partition("=")
            key = key.strip()
            if not found or not key or " " in key:
                continue
            self.assertIn(key, self.config_keys,
                          "%s names %s, which is not a setting" % (EXAMPLE, key))


class LinkTest(unittest.TestCase):
    def test_every_cross_reference_resolves(self):
        """Splitting one document into four made these easy to get wrong."""
        for name in DOCS:
            base = os.path.dirname(os.path.join(ROOT, name))
            for target in re.findall(r"\]\((?!http)([^)#]+)", read(name)):
                self.assertTrue(
                    os.path.exists(os.path.normpath(
                        os.path.join(base, target))),
                    "%s links to %s, which does not exist" % (name, target))


class FileTest(unittest.TestCase):
    """Files named in the prose, against the ones that are there."""

    # Where a name has to resolve to something; /run, /var and ~/.config
    # are not in the repository and are checked by reading, not by test.
    OURS = ("bin/", "btkey/", "docs/", "examples/", "layouts/", "tests/",
            "tools/")

    def test_every_repository_path_named_exists(self):
        for name in DOCS + [EXAMPLE]:
            for token in re.findall(r"`([^`\s]+)`", read(name)):
                if not token.startswith(self.OURS):
                    continue
                self.assertTrue(
                    os.path.exists(os.path.join(ROOT, token.rstrip("/"))),
                    "%s names %s, which does not exist" % (name, token))

    def test_the_worked_example_is_named_correctly(self):
        """A layout is referred to by file name, and those names moved."""
        real = os.listdir(os.path.join(ROOT, "layouts"))
        stems = {found.split(".")[0] for found in real}
        for name in DOCS:
            for token in re.findall(r"`([^`\s]+)`", read(name)):
                stem = os.path.basename(token).split(".")[0]
                if stem not in stems or "." not in token:
                    continue
                self.assertIn(
                    os.path.basename(token), real,
                    "%s names %s, which is not in layouts/" % (name, token))


class SourceTableTest(unittest.TestCase):
    def test_every_module_is_in_the_readme(self):
        """A new module is easy to add and easy to forget to mention."""
        readme = read("README.md")
        section = readme[readme.index("## Source"):
                         readme.index("## Not implemented")]
        listed = set(re.findall(r"`(\w+\.py)`", section))
        present = {name for name in os.listdir(os.path.join(ROOT, "btkey"))
                   if name.endswith(".py")}
        self.assertEqual(present - listed, set(),
                         "not named in the source table")
        self.assertEqual(listed - present, set(),
                         "named in the source table but not there")


class ExampleTest(unittest.TestCase):
    """The example files, and the documentation that quotes them."""

    DROP_IN = "examples/50-no-hfp.conf"

    def settings(self, text):
        return [line for line in text.splitlines()
                if line.strip() and not line.startswith("#")]

    def test_the_documentation_quotes_the_drop_in_as_it_is(self):
        """Two documents show its contents; the file is the original.

        DESIGN.md once showed a role list the file had stopped having, and
        following it would have cost a re-pair to find out.
        """
        wanted = self.settings(read(self.DROP_IN))
        for name in DOCS:
            for block in re.findall(r"```\n(monitor\.bluez\.properties.*?)```",
                                    read(name), re.S):
                self.assertEqual(block.strip().splitlines(), wanted,
                                 "%s quotes something else" % name)

    @staticmethod
    def header(text):
        """The comment block at the top, where placement belongs.

        Looking at the whole file would find the path in some example
        value further down and call that an instruction.
        """
        lines = []
        for line in text.splitlines():
            if line.strip() and not line.startswith("#"):
                break
            lines.append(line)
        return "\n".join(lines)

    def test_each_example_says_where_it_belongs(self):
        # An example config nobody can place is a puzzle, not an example.
        for name, destination in ((EXAMPLE, "~/.config/btkey/btkey.conf"),
                                  (self.DROP_IN, "wireplumber.conf.d/")):
            self.assertIn(destination, self.header(read(name)),
                          "%s does not say at the top where it goes" % name)


class LicenceTest(unittest.TestCase):
    """Every source file says what it is licensed under.

    A file without the tag is one somebody has to guess about, and the
    guess a stranger makes about an untagged file in a GPL repository is
    not always the right one.
    """

    TAG = "SPDX-License-Identifier: GPL-2.0-only"

    def sources(self):
        """Every source file that is here.

        Named rather than globbed for the loose ones, but only required to
        be tagged if present: a release leaves `release` itself behind, and
        a checkout of the published branch has to pass its own tests.
        """
        import glob
        found = (glob.glob(os.path.join(ROOT, "btkey", "*.py"))
                 + glob.glob(os.path.join(ROOT, "tests", "*.py"))
                 + glob.glob(os.path.join(ROOT, "tools", "*")))
        found += [os.path.join(ROOT, name)
                  for name in ("Makefile", "bin/btkey", "tests/run",
                               "release")]
        return sorted(path for path in found if os.path.exists(path))

    def test_every_source_file_is_tagged(self):
        for path in self.sources():
            with open(path, encoding="utf-8") as handle:
                head = "".join(handle.readlines()[:3])
            self.assertIn(self.TAG, head,
                          "%s has no licence tag" % os.path.relpath(path, ROOT))

    def test_the_tag_comes_after_any_shebang(self):
        # A tag above #! would stop the file being executable at all.
        for path in self.sources():
            with open(path, encoding="utf-8") as handle:
                first = handle.readline()
            if first.startswith("#!"):
                continue
            self.assertNotIn("#!", first, path)

    def test_the_licence_itself_is_there(self):
        with open(os.path.join(ROOT, "LICENSE"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("GNU GENERAL PUBLIC LICENSE", text)
        self.assertIn("Version 2, June 1991", text)


class PunctuationTest(unittest.TestCase):
    def test_no_em_dashes(self):
        """A colon, a comma, a semicolon or a bracket says it as well.

        The code has never had one; the prose is held to the same rule.
        """
        for name in DOCS + [EXAMPLE]:
            for number, line in enumerate(read(name).splitlines(), 1):
                self.assertNotIn(
                    "\u2014", line,
                    "%s:%d has an em dash: %s" % (name, number, line.strip()))


class PathTest(unittest.TestCase):
    def test_no_absolute_home_directories(self):
        """Someone else's home directory is not an example anyone can use."""
        for name in DOCS + ["examples/btkey.conf.example"]:
            self.assertNotIn("/home/", read(name),
                             "%s has an absolute home directory" % name)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
