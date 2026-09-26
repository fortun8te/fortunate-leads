# Fortunate Leads

Finds and ranks Instagram leads for Fortunate from the follower and following lists of chosen accounts.
A local server keeps the database and the web app; a Chrome extension, one per Instagram account, reads the
lists and bios at human pace. Ranking weighs the network first (how many of your lists someone is in, who follows
them, links to you and your clients), then the profile read by rules or a free LLM.

- Setup, first run, backups and troubleshooting: [docs/SETUP.md](docs/SETUP.md)
- API and database: [docs/CONTRACT.md](docs/CONTRACT.md)
- Current state: [docs/HANDOFF.md](docs/HANDOFF.md)
- Connection mapping research and design: [docs/CONNECTION_MAPPING.md](docs/CONNECTION_MAPPING.md)

The Map page opens a connection explorer. Pick two accounts to inspect recorded follow paths, their directions,
source lists and collection coverage. These paths describe imported observations, not confirmed relationships.
The network overview remains available in the adjacent tab.

```sh
python3 server/server.py    # then open http://127.0.0.1:8777
```
