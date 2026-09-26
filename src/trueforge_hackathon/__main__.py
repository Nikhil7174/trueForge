from __future__ import annotations

import sys


def main() -> None:
    usage = "usage: python -m trueforge_hackathon {mcp|gate|ship|seed|run} ..."
    if len(sys.argv) < 2:
        raise SystemExit(usage)
    command, rest = sys.argv[1], sys.argv[2:]
    sys.argv = [sys.argv[0], *rest]
    if command == "mcp":
        from trueforge_hackathon.agents.access_reviewer.mcp_server import main as mcp_main

        mcp_main()
        return
    if command == "gate":
        from trueforge_hackathon.agents.migration_rehearsal.gate_server import main as gate_main

        gate_main()
        return
    if command == "ship":
        from trueforge_hackathon.agents.release_captain.ship_gate import main as ship_main

        ship_main()
        return
    if command == "seed":
        from trueforge_hackathon.cli.seed import main as seed_main

        seed_main()
        return
    if command == "run":
        from trueforge_hackathon.cli.run import main as run_main

        run_main()
        return
    raise SystemExit(usage)


if __name__ == "__main__":
    main()
