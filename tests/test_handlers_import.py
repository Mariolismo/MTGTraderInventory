"""Smoke: Lambda handler modules must import (catches SyntaxError before deploy)."""

from __future__ import annotations

import compileall
import unittest
from pathlib import Path


class HandlerImportTests(unittest.TestCase):
    def test_aws_package_compiles(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "cardtrader_inventory"
        ok = compileall.compile_dir(str(root), quiet=1)
        self.assertTrue(ok, "cardtrader_inventory failed to compile")

    def test_handlers_import(self) -> None:
        from cardtrader_inventory.aws import handlers

        for name in (
            "prepare_handler",
            "plan_chunk_handler",
            "merge_handler",
            "apply_handler",
        ):
            self.assertTrue(callable(getattr(handlers, name)))


if __name__ == "__main__":
    unittest.main()
