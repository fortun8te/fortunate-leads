# Lead workflow research, 26 September 2026

## Decision

Build four improvements around the existing lead record: dated follow-ups, an activity history, filtered or selected CSV export, and profile freshness with a refresh action. Fix the note-saving and saved-view feedback bugs encountered in those flows.

These are confirmed implementation gaps at baseline commit `9bac874`, not claims about customer demand or revenue. Official product documentation establishes that the underlying workflows exist in comparable relationship tools. Their priority here follows from this application's own Contacted, Talking and Client stages and its note prompt asking users to write down when to follow up. No customer interviews, usage analytics or controlled outcome measurements were available.

## What already exists

The repository already implements network-based Instagram discovery, paced collection, multiple account lanes, scoring and quoted evidence, a relationship map, five pipeline statuses, manual tags, bulk changes with undo, saved views, website research, backups and collection recovery. Building another version of those would not fill a gap.

Audited evidence: `README.md`, `docs/CONTRACT.md`, `docs/HANDOFF.md`, `server/db.py`, `server/server.py`, `server/qual_api.py`, `web/app.js`, `web/index.html`, and the server and extension tests. Both frontend and backend audits independently identified the first three gaps below.

## Evidence and implementation scope

| Capability | Observed gap in this repository | External evidence checked on 26 September 2026 | Small useful implementation |
| --- | --- | --- | --- |
| Follow-ups | The `marks` record holds status, note and update time. There is no structured due date or due filter. The lead note prompt explicitly mentions following up. | [Attio tasks](https://attio.com/help/reference/productivity-collaborating/tasks) supports tasks tied to records, due-date sorting, completion and views for today or overdue work. | One current follow-up per lead, with a calendar date and optional next action. Schedule, reschedule, complete or clear it. Filter due, overdue, scheduled, completed or no follow-up and sort by date. |
| Activity history | Status and note edits replace the current values. There is no durable list of interactions or past changes. | [folk manual interactions](https://help.folk.app/en/articles/7012167-log-a-new-interaction) records interaction type, date and description on the contact. [folk interaction history](https://help.folk.app/en/articles/5007315-track-interactions-emails-calendar-events-whatsapp-conversations) keeps prior relationship context with the contact. | A dated timeline for manual DMs, replies, calls, meetings and notes, plus actual status, note and follow-up changes. Keep the current note separately. Load older entries in pages. |
| CSV export | `/api/leads` returns at most 500 JSON rows per request. There is no CSV route or download action. Database backup already exists but serves a different purpose. | [folk export](https://help.folk.app/en/articles/5007388-export-data) respects the current view's filters and exports contact information and notes. | Download either the full filtered result or exactly the selected leads. Include useful profile, qualification, tag, status, note and follow-up information. Screen pagination must not truncate exports. |
| Profile freshness and refresh | `bio_at` is stored and visible in the qualification inspector. `/api/person/:id/read` already supports another read. The main Leads panel hides its read button once a bio exists. | Direct code evidence is sufficient. This is an incomplete existing workflow, not a newly discovered Instagram capability. | Show the last profile read in the lead panel and expose Refresh profile. Reuse the existing paced queue and show when a read is pending. |

The first three sources document product behavior, not measured benefits. The two folk interaction pages corroborate one provider's behavior; they are not independent customer validation. No source establishes automatic Instagram DM synchronization.

## Countercase and scope decisions

The strongest objection is that a discovery tool should not grow into a full CRM. That objection is valid for automated sequences, team assignment, sales forecasting and a second pipeline interface. It is weaker for a date beside an existing Contacted lead or a record of an earlier reply. The implementation therefore stays inside the current lead list and detail panel.

Candidates researched but not selected:

- **CSV import.** [Pipedrive's official import guide](https://support.pipedrive.com/en/article/importing-data-into-pipedrive-with-spreadsheets) demonstrates validation, mapping, preview and duplicate handling. This repository collects its records through Instagram and has no supplied external dataset or migration requirement. Import would introduce a new ingestion workflow with unresolved identity and overwrite rules. Export is supported by the existing workflow without those assumptions.
- **A separate Do not contact flag.** [HubSpot's opt-out import](https://knowledge.hubspot.com/marketing-email/import-an-opt-out-list) demonstrates durable email suppression. Extending that behavior to Instagram is a design inference, not a documented cross-channel rule. This app sends no outreach, and Not a fit already hides leads by default. A distinct suppression mechanism should precede any future sending feature; it is not presented as part of this update.
- **Automatic messaging or conversation synchronization.** The research does not establish an authorized Instagram integration for this repository. Manual logging provides a complete, useful interaction record without making that claim.
- **Another pipeline board, automated enrichment or backups.** The underlying pipeline, qualification and backup capabilities already exist. No evidence identified a new board as more useful than completing follow-up and history support.

## Reliability requirements

- Follow-up dates are calendar dates. Due includes today; overdue excludes today. The browser supplies its local date when requesting relative views. Saved views must not freeze the date when saved.
- History begins with this update. Do not invent historical events from a current status or note. No-op saves must not create duplicate change events. Bulk status changes and undo must be recorded.
- Preserve the current note and every new history entry when identities merge. Conflicting reminders need a deterministic choice and a recorded explanation.
- Serialize note saves per person. Switching profiles must not cancel another person's draft; an older response must not mark newer text saved. Retain failed drafts for retry.
- Exports reuse the same filter rules as the list. Selected exports have a separate, explicit scope. Empty exports still produce column headings, and invalid requests fail visibly.
- Quote CSV correctly and guard formula-like user content. [OWASP CSV injection guidance](https://owasp.org/www-community/attacks/CSV_Injection) explains why CSV quoting alone is insufficient and why no escaping convention is universal across spreadsheet programs. CSV is a portable lead snapshot, not a complete database backup.
- Profile refresh uses the existing queue, budgets and cooldowns. The update does not increase collection speed.

## Validation

Acceptance includes persistence after reopening the database, date boundaries, shared filters, large exports, CSV escaping, timeline pagination, note-save races and identity merges. Existing server, extension and simulated collection checks protect the surrounding workflows. Browser checks use a separate fixture database, not the user's live leads. The pull request records the final checks and any remaining limitations.
