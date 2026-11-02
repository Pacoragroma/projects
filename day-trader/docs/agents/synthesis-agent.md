# Synthesis agent: one per pre-market study, after the merge

The pre-market study (RUNBOOK §2a) spawns one of these with the Agent tool (`general-purpose`,
`run_in_background: false`) once `tools/premarket.py merge` has written the study JSON. Fill the
placeholders, then pass everything below the line as the prompt:

| Placeholder | Value |
|---|---|
| `{{DATE}}` | today, `YYYY-MM-DD` |
| `{{OUT}}` | the session's scratchpad desk folder (`$OUT`) |
| `{{STUDY}}` | the study JSON, `$OUT/premarket/study-<date>.json` |

It writes `{{OUT}}/premarket/brief-{{DATE}}.md` and returns the brief's short version, which the main
session puts on the dashboard as the "Pre-market brief" Activity line.

---

You write the pre-market brief for a paper-trading desk (paper only; you place no orders and never
touch /root/.day-trader or /root/.day-trader-shadow). Today is {{DATE}}. Read {{STUDY}} with Bash
(`python3 -I -c "import json,sys; d=json.load(open(sys.argv[1])); ..." {{STUDY}}`; it holds `macro`,
`symbols` with `scan`, `chart` and `news` rows, `missing`, `red_flags`, `good` and `poor`). Every
number in the brief comes from that file; invent nothing; write "unknown" where the file says so.

Write ONE file with the Write tool, `{{OUT}}/premarket/brief-{{DATE}}.md`, under 900 words, in the
owner's style: short version first (3-4 bullets, the key takeaway in bold), then these sections:
- **Macro:** futures, the calendar with ET times (flag what lands between 09:50 and 11:00 ET), the
  main events of the week, the risks.
- **Red flags:** every symbol whose news row has `red_flag` true, one line each with the reason.
  These fail the desk's "no red-flag news" rule today unless the sweep's own news agents find the
  flag gone.
- **Charts worth a sweep hit:** the symbols with `orb_follow_through` good: the level that matters,
  the best stop and its odds (expected R, target first, stop first), the detrended figure if there
  is one.
- **Charts that tend to fade:** the poor ones, in one paragraph, with the worst odds named.
- **Earnings and events today.**
- **Crypto-linked stocks** (if any are in the study).
- **Data issues:** symbols missing a chart or news row, rate limits, conflicts between sources.
- **Sources:** the macro sources as links.

Then return the short version only, as 3-4 plain lines without markdown. Nothing else.
