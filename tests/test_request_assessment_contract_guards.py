from __future__ import annotations

import ast
from pathlib import Path
import unittest


class RequestAssessmentContractGuardTests(unittest.TestCase):
    def test_forbidden_runtime_imports_are_absent(self) -> None:
        forbidden = {"GoNoGoService", "CommercialOffersService", "RetrievalService", "AtaImpactService", "AtaImpactAgent"}
        root = Path("core/request_assessment")
        for path in list(root.rglob("*.py")) + [Path("apps/request_assessment/server.py")]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names = {alias.name for alias in node.names}
                    self.assertFalse(forbidden & names, f"{path} imports {forbidden & names}")
                if isinstance(node, ast.Import):
                    names = {alias.name.rsplit(".", 1)[-1] for alias in node.names}
                    self.assertFalse(forbidden & names, f"{path} imports {forbidden & names}")


if __name__ == "__main__":
    unittest.main()

