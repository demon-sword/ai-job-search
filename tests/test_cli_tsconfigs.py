"""Guards for portal CLI tsconfig consistency.

Regression test for the red cli-checks run: the Danish CLIs set
``"lib": ["ESNext"]`` (no DOM), which routes bun-types' web globals
(Response/Headers/Request/URL) through an ``import("undici-types")``
fallback. That package is only present transitively (via modern
@types/node); under installer layouts resolving @types/node 16 it is
absent, the fallback dangles, and ``tsc --noEmit`` fails with TS2339 /
TS2749. Declaring the DOM lib (like the passing siblings, whose default
lib includes it) provides those members unconditionally.

The ``types`` entry must name the direct ``@types/bun`` dependency
(``"bun"``), never the transitive ``bun-types`` package it happens to
re-export, whose top-level hoisting is not guaranteed.
"""

import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _cli_tsconfigs():
    return sorted(REPO.glob(".agents/skills/*/cli/tsconfig.json"))


class CliTsconfigConsistency(unittest.TestCase):
    def test_every_cli_resolves_full_web_globals(self):
        configs = _cli_tsconfigs()
        self.assertGreater(len(configs), 0, "no CLI tsconfigs discovered")
        for path in configs:
            with self.subTest(cli=path.parent.parent.name):
                options = json.loads(path.read_text(encoding="utf-8"))["compilerOptions"]
                types = options.get("types", None)
                self.assertTrue(
                    types is None or "bun" in types,
                    f"{path}: types entry must include the direct @types/bun dep: {types}",
                )
                self.assertNotIn(
                    "bun-types",
                    types or [],
                    f"{path}: must not rely on the transitively-hoisted bun-types package",
                )
                lib = options.get("lib", None)
                self.assertTrue(
                    lib is None or "DOM" in lib,
                    f"{path}: without the DOM lib, web globals depend on "
                    "transitive undici-types (absent with @types/node 16)",
                )


if __name__ == "__main__":
    unittest.main()
