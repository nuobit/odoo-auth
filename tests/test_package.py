"""Tests of the package as it is installed and imported: the one command its
pyproject declares, and the import of each of its modules in a Python of its
own, with -B and -W error, so that it is the first import of its process,
and any warning fails it.

Run from the repository root:
    PYTHONPATH=src python3 -m unittest discover -s tests
"""

import os
import pkgutil
import tomllib
import unittest

import odoo_auth.cli
from support import run_python

PYPROJECT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "pyproject.toml"
)


class CommandTest(unittest.TestCase):
    def test_the_one_command_runs_the_main_of_the_cli(self):
        with open(PYPROJECT, "rb") as f:
            scripts = tomllib.load(f)["project"]["scripts"]
        self.assertEqual(list(scripts), ["odoo-auth"])
        self.assertIs(pkgutil.resolve_name(scripts["odoo-auth"]), odoo_auth.cli.main)


class ImportTest(unittest.TestCase):
    def test_each_module_imports_alone_and_prints_nothing(self):
        for module in ("odoo_auth", "odoo_auth.client", "odoo_auth.cli"):
            with self.subTest(module=module):
                run = run_python("-c", f"import {module}")
                self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
