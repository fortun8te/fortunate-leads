# Fortunate Leads

Finds and ranks Instagram leads for Fortunate from the follower and following lists of chosen accounts.
A local server keeps the database and the web app. A Chrome extension, one per Instagram account, reads
lists and can read bios. An optional logged-out collector reads public profile bios through configured
connections; it does not collect follower or following lists. Ranking uses network links and profile evidence,
with rules and optional free-model AI qualification.

- Setup, first run, backups and troubleshooting: [docs/SETUP.md](docs/SETUP.md)
- API and database: [docs/CONTRACT.md](docs/CONTRACT.md)
- Current state: [docs/HANDOFF.md](docs/HANDOFF.md)
- Public bio collector setup and operation: [docs/SCRAPER_OPERATIONS.md](docs/SCRAPER_OPERATIONS.md)

```sh
python3 server/server.py    # then open http://127.0.0.1:8777
```
