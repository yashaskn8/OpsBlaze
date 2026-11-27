# OpsBlaze console design

## Direction

The incident register leads. Read the target, state and version together, then inspect the evidence that supports the next decision. The record-to-decision sequence is the signature structure. It follows server evidence and retains uncertain outcomes and failed lookups in place.

Three directions were considered. A chronological journal had the clearest story for retrospectives but made queue work and incident switching slow. A full-screen operations wall gave queue signals prominence, at the expense of readable approval rationale and accessible row selection. The incident register offers the shortest path from admission to investigation and review, with smaller isolated workspaces for secondary tasks.

## Foundations

| Role | Value | Use |
| --- | --- | --- |
| Canvas | `#eff1ee` | Console background |
| Paper | `#fafbf8` | Records and form surfaces |
| Graphite | `#1e2825` | Text and workspace rail |
| Muted ink | `#52625a` | Secondary labels and metadata |
| Rule | `#ccd4cc` | Boundaries between records |
| Signal | `#8a461e` | Primary action and active review |

Use Segoe UI for labels and prose, Barlow Semi Condensed for page titles, and Consolas for identifiers and digests. Use tabular figures for age, counts and version. Body is 15–16 px, labels 13–14 px and detail headings 21–26 px. Distinguish severity with words and treatment together. Use muted green for affirmed state, brown for pending or uncertain state and red for a refusal or failure.

Use a four-pixel spacing scale, crisp record rules and small control radii. Focus is a visible three-pixel outline. Forms use explicit labels above inputs, with reasons next to the action they govern. Buttons stay at least 36 px high, or 44 px on narrow screens. Request statuses and authoritative results have separate text. Keep long digests and trace IDs selectable and allow them to wrap.

Desktop uses a full-width shell with a graphite navigation rail, a wide register and a distinct intake column. Wide displays keep a readable inner content measure. The incident trace gets its own workspace and uses the page scrollbar for records and links. A replay table can scroll horizontally when its columns need more room. Tablet puts the rail across the top. Narrow layouts put intake, snapshot and register in reading order, with table rows adapting to labels and values. Orient with an active link, visible workspace title, and tenant context.

Once an incident is open, intake collapses to its named action. Reopening intake retains the session fields and request key. The desktop register sits beside the active incident, so switching incidents remains close to the pending decision.

Use semantic tables for records and comparisons, definition lists for facts, and native buttons, inputs and disclosure controls. Use brief state descriptions for loading, stale, unavailable and empty content. Avoid decorative charts, invented activity, heavy gradients and nested cards. Motion is limited to direct feedback, and reduced-motion preferences remove transitions.
