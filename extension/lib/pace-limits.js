// The one place for pace. Defaults, bounds and presets live here and nowhere else: the extension (lib/core.js),
// the server (server/pace_limits.py), ops/set-pace and the tests all read this file.
//
// To install evidence-backed bounds, edit ONLY the numbers in the JSON block below. Keep it strict JSON (double
// quotes, no comments, no trailing commas): the server parses the text between the two marker lines.
//   floor / ceiling  for a range: [bottom end, top end] pairs. The bottom of a range must stay between floor[0] and
//                    ceiling[0], the top between floor[1] and ceiling[1], whatever is asked for. A plain number for a count.
//   min_spread       the top of a range must be at least this many times its bottom (the range is the jitter).
//   default          what runs when nothing is set. Changing a default changes real pacing: do it only with evidence.
//   budget_max       the highest daily list pages / bio reads a pace command may set. 0 = no daily cap.
//   budget_default   what the pace control treats as normal: no daily cap. Stored values are never rewritten by this.
// Units: seconds for time, pages or requests for counts.
//
// status "provisional": the fast side of every bound equals today's default, so nothing can go faster than the
// shipped pacing until the pacing analysis widens these numbers. The slow side is free. The preset "fast" asks for
// faster-than-default values and is clamped to the bounds, so it equals "normal" until the bounds are widened.
(function (root) {
  // BEGIN-PACE-LIMITS-JSON
  const LIMITS = {
    "version": 1,
    "status": "provisional",
    "note": "Only the list gap can go faster than the shipped default, and only as far as the ramp needs to reach 285 list pages an hour (its proven ceiling). Every other bound still equals the shipped default.",
    "fields": {
      "list_gap":    { "label": "Seconds between list pages",             "unit": "s",        "kind": "range", "step": 0.1, "default": [7, 12],   "floor": [4.8, 8.2], "ceiling": [96, 120],  "min_spread": 1.25 },
      "profile_gap": { "label": "Seconds between bio reads",              "unit": "s",        "kind": "range", "step": 0.1, "default": [35, 70],  "floor": [35, 70],  "ceiling": [288, 360], "min_spread": 1.25 },
      "spacing":     { "label": "Seconds between any two requests",       "unit": "s",        "kind": "range", "step": 0.1, "default": [2, 5],    "floor": [2, 5],    "ceiling": [48, 60],   "min_spread": 1.25 },
      "break_every": { "label": "List pages between breaks",              "unit": "pages",    "kind": "range", "step": 1,   "default": [40, 60],  "floor": [10, 13],  "ceiling": [40, 60],   "min_spread": 1.25 },
      "break_len":   { "label": "Seconds per break",                      "unit": "s",        "kind": "range", "step": 1,   "default": [90, 180], "floor": [90, 180], "ceiling": [720, 900], "min_spread": 1.25 },
      "window_max":  { "label": "Requests in any 11 minutes (a ceiling)", "unit": "requests", "kind": "int",   "step": 1,   "default": 72,        "floor": 12,       "ceiling": 72 }
    },
    "budget_max": { "list": 3000, "profile": 5000 },
    "budget_default": { "list": 0, "profile": 0 },
    "presets": {
      "slow":   { "label": "Slow",   "values": { "list_gap": [14, 24], "profile_gap": [70, 140], "spacing": [4, 10], "break_every": [20, 30], "break_len": [180, 360], "window_max": 36 } },
      "normal": { "label": "Normal", "values": {} },
      "fast":   { "label": "Fast",   "values": { "list_gap": [5, 9], "profile_gap": [26, 52], "spacing": [1.5, 4], "break_every": [50, 75], "break_len": [68, 135], "window_max": 72 } }
    }
  };
  // END-PACE-LIMITS-JSON
  const freeze = (o) => { if (o && typeof o === 'object') { Object.values(o).forEach(freeze); Object.freeze(o); } return o; };
  const api = freeze(LIMITS);
  root.FLPaceLimits = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof globalThis !== 'undefined' ? globalThis : this);
