/* Built-in due view and plain-text list labels; no DOM or saved state. */
(function (root) {
  const DUE_QUERY = 'status=all&follow_up=due&sort=follow_up';

  // Parse this with the app's fromQuery() to replace, rather than narrow, filters.
  function dueQuery() { return DUE_QUERY; }

  function isDueView(f, sort) {
    return !!f && f.status === 'all' && f.follow_up === 'due' && sort === 'follow_up'
      && !f.tags?.length && !f.any?.length && !f.not?.length
      && !f.tier && !f.q && !f.min && !f.bio && !f.seed
      && f.fmin == null && f.fmax == null;
  }

  function localToday() {
    const date = new Date();
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  }

  // Text stays unescaped here. Escape it when inserting it into HTML.
  function rowAction(person, today = localToday()) {
    const followUp = person?.follow_up;
    if (!followUp || followUp.completed_at || !followUp.due_on) return null;
    const note = typeof followUp.note === 'string' ? followUp.note.replace(/\s+/g, ' ').trim() : '';
    const dueOn = followUp.due_on;
    const overdue = dueOn < today;
    return {
      text: note || 'Follow up',
      label: note ? `Next: ${note}` : 'Follow up',
      dueLabel: overdue ? `Overdue · ${dueOn}` : dueOn === today ? 'Due today' : `Due ${dueOn}`,
      dueOn,
      overdue,
    };
  }

  function context(f, sort) {
    return isDueView(f, sort) ? 'Due today or earlier · All statuses · Oldest first' : '';
  }

  function empty(f, sort) {
    return isDueView(f, sort) ? {
      title: 'No follow-ups due',
      detail: 'Future follow-ups appear here when they are due.',
      action: 'View open leads',
    } : null;
  }

  root.LeadDaily = { dueQuery, isDueView, rowAction, context, empty };
})(typeof window === 'undefined' ? globalThis : window);
