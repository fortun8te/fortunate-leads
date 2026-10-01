# Fortunate Leads

Finds and ranks Instagram leads for Fortunate from the follower and following lists of chosen accounts.
A local server keeps the database and the web app. A Chrome extension, one per Instagram account, reads
lists and can read bios. Ranking uses network links and profile evidence, with rules and optional free-model
AI qualification.

- What this is and why: [docs/PRODUCT.md](docs/PRODUCT.md)
- Setup, first run, backups and troubleshooting: [docs/SETUP.md](docs/SETUP.md)
- API and database: [docs/CONTRACT.md](docs/CONTRACT.md)
- Connection mapping research and design: [docs/CONNECTION_MAPPING_RESEARCH.md](docs/CONNECTION_MAPPING_RESEARCH.md)
- Pair comparison API: [docs/CONNECTIONS.md](docs/CONNECTIONS.md)
- Current state: [docs/HANDOFF.md](docs/HANDOFF.md)
- Lead follow-ups, activity and exports: [docs/LEAD_WORKFLOWS.md](docs/LEAD_WORKFLOWS.md)
- Feature research and scope decisions: [docs/FEATURE_RESEARCH.md](docs/FEATURE_RESEARCH.md)
- UI polish research, comparisons and checks: [docs/UI-POLISH.md](docs/UI-POLISH.md)

New Mac: `ops/install.sh`, then follow Get started in the web app. By hand: `python3 server/server.py` and open http://127.0.0.1:8777.

## Connection map

The map can load precomputed binary tiles through WebGPU or WebGL2. See [map setup and protocol](docs/MAP_UNIVERSE_API.md), [measured limits](docs/MAP_VIEW_PERFORMANCE.md), and [agent handoff](docs/HANDOFF.md) before changing the renderer or building a large snapshot. The current browser working set is bounded; five-million-node performance has not been measured.
