# Follow-ups, history and exports

Open any person in Leads or Map to use these controls. Existing leads and notes remain available after updating; the local database adds the new tables when the server starts.

## Follow up on a lead

In the detail panel, choose a date under **Follow-up**, optionally describe the next action, then choose **Schedule**. Each person has one current reminder. **Reschedule** changes it, **Complete** marks it done, and **Clear** removes the current reminder. Earlier changes remain in Activity.

Use the Follow-up filters in the list sidebar:

- **Due today or earlier:** all unfinished reminders dated today or before.
- **Overdue:** unfinished reminders before today.
- **Scheduled:** all unfinished reminders, including future dates.
- **Completed:** people whose current reminder is complete.
- **None:** people without a current reminder.

These filters combine with status, tags, search and the other existing filters. Not a fit stays hidden under the default Open status filter. Select **Follow-up date** in Sort to put the earliest unfinished reminders first. Save a view to reopen the same filter; its meaning updates with the browser's local calendar date.

Reminders are an in-app list. They do not send messages, desktop notifications or emails while the app is closed.

## Keep relationship history

Under **Activity**, choose DM sent, Reply received, Call, Meeting or Note, enter when it happened and add the details. **Log activity** saves it to that person's timeline. The entered time uses your local timezone and is stored as an absolute timestamp. Use **Load older activity** for earlier entries.

Status changes, saved note edits and reminder changes also appear automatically. History starts when this update is installed; it cannot reconstruct past changes that the previous version overwrote. The current Note remains a separate editable summary. Re-saving the same value does not add another change event.

Notes save independently for each person, including when switching leads. Failed drafts remain in the browser for retry and are never labelled Saved. Export waits for pending notes and reports an error if a note cannot be saved.

When two records resolve to the same Instagram identity, their history moves to the retained person. An unfinished reminder takes precedence over a completed one, then the earlier date wins; conflicts are recorded in the timeline. Distinct notes are combined when they fit the existing note limit. If they do not, the current note stays editable and the other full note is retained in the merge history.

## Workspace backup

Use the existing database backup procedure or `ops/export_workspace.py` for a complete workspace snapshot. Existing saved-view data remains in the database for recovery. Bulk selection, saved views and in-app CSV downloads were removed from the app.

## Refresh profile information

The Profile section shows when the bio was last read and its source. **Refresh profile** queues another read even when a bio already exists. **Refresh queued** means a read is waiting or in progress. It uses the existing extension queue, account budgets and cooldowns; an online account must be available for the read to finish.
