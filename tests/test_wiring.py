#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Every name the code uses, against the names it has.

Python resolves an attribute when the line runs, not when the file loads,
so a method that is called but never defined is invisible until that path
is taken - and the paths taken least often are the error paths, which are
exactly where nobody is watching.

This is not hypothetical.  A commit removed the runtime main.conf writer,
which had stopped being useful, and took _noplugin() with it while leaving
both calls in place.  Starting the private bluetoothd raised AttributeError
from then on, and since that is not a BluetoothdError it went past the
handler that would have explained it.

It happened again, differently: a function was moved into btlink and the
call to it went in, but the definition was lost to a botched edit.  Every
test passed, because nothing but the real startup path calls it.  So the
same question is asked across modules as well as within them.

A type checker would say all this and more.  This is the part that can be
had for a page of ast, run by the same command as everything else.
"""

import ast
import builtins
import glob
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The tools are Python and call into the package, so they are exposed to
# the same mistake this file exists to catch; only the bash one is not.
SOURCES = (sorted(glob.glob(os.path.join(ROOT, "btkey", "*.py")))
           + [os.path.join(ROOT, "bin", "btkey"),
              os.path.join(ROOT, "tools", "btkey-trace-input")])


def parse(path):
    with io.open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), path)


def self_attributes(cls):
    """(what the class provides, what it asks of itself)."""
    provided, used = set(), {}
    for node in ast.walk(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            provided.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    provided.add(target.id)     # a class attribute
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            provided.add(node.target.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id == "self":
            if isinstance(node.ctx, ast.Store):
                provided.add(node.attr)
            else:
                used.setdefault(node.attr, node.lineno)
    return provided, used


class SelfTest(unittest.TestCase):
    def test_every_self_attribute_exists(self):
        for path in SOURCES:
            tree = parse(path)
            local = {node.name for node in ast.walk(tree)
                     if isinstance(node, ast.ClassDef)}
            for cls in [n for n in ast.walk(tree)
                        if isinstance(n, ast.ClassDef)]:
                # A base we cannot see here - dbus.service.Object, say -
                # may be providing the name.
                if any(not isinstance(base, ast.Name) or base.id in local
                       for base in cls.bases):
                    continue
                provided, used = self_attributes(cls)
                for attr, line in sorted(used.items(), key=lambda kv: kv[1]):
                    if attr.startswith("__"):
                        continue
                    self.assertIn(
                        attr, provided,
                        "%s:%d %s.self.%s is used but never defined "
                        "or assigned" % (os.path.relpath(path, ROOT), line,
                                         cls.name, attr))


class ModuleTest(unittest.TestCase):
    """Bare names: imports that went away, helpers that were renamed."""

    def bound(self, tree):
        # Module globals the interpreter provides but builtins does not.
        names = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                names.add(node.name)
                args = getattr(node, "args", None)
                if args is not None:
                    for group in (args.posonlyargs, args.args, args.kwonlyargs):
                        names.update(arg.arg for arg in group)
                    for extra in (args.vararg, args.kwarg):
                        if extra:
                            names.add(extra.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.Global):
                names.update(node.names)
            elif isinstance(node, (ast.comprehension,)):
                pass
        return names

    def test_every_name_used_is_bound_somewhere(self):
        for path in SOURCES:
            tree = parse(path)
            names = self.bound(tree)
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                    self.assertIn(
                        node.id, names,
                        "%s:%d uses %s, which is bound nowhere in the file"
                        % (os.path.relpath(path, ROOT), node.lineno, node.id))



class SiblingAttributeTest(unittest.TestCase):
    """Every btkey.thing.name the code mentions, against what is there.

    The within-a-file check above cannot see this: `btlink` is bound in
    cli.py by the import, so `btlink.use_glib_mainloop()` looks fine
    there whatever btlink actually holds.  Only the module itself can
    answer, and importing btkey is cheap - the tests do it anyway.
    """

    PACKAGE = "btkey"

    def modules(self):
        """The package's own modules, by the name others import them as."""
        import importlib
        found = {}
        for path in SOURCES:
            name = os.path.basename(path)
            if not name.endswith(".py") or name == "__init__.py":
                continue
            stem = name[:-3]
            found[stem] = importlib.import_module("%s.%s"
                                                  % (self.PACKAGE, stem))
        return found

    def test_every_sibling_attribute_exists(self):
        known = self.modules()
        for path in SOURCES:
            tree = parse(path)
            # Only the names this file actually imports from the package;
            # a local variable called `probe` is not the module.
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    imported |= {alias.asname or alias.name
                                 for alias in node.names}
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(self.PACKAGE + "."):
                            imported.add(alias.asname
                                         or alias.name.split(".")[-1])
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                if not isinstance(node.value, ast.Name):
                    continue
                module = known.get(node.value.id)
                if module is None or node.value.id not in imported:
                    continue
                self.assertTrue(
                    hasattr(module, node.attr),
                    "%s:%d uses %s.%s, which %s does not have"
                    % (os.path.relpath(path, ROOT), node.lineno,
                       node.value.id, node.attr, node.value.id))


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
