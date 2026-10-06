# `familysearch`: read-only FamilySearch

A second command in the same package, built the same way as `ancestry`: your own signed-in Chrome does the requests, nothing is
stored, one JSON object per command, exit 0 ok / 1 failed / 2 bad usage. Reads only; tree writes are out of scope.

## Setup
A Chrome with `--remote-debugging-port=9223` and its own profile, **exactly one tab, on www.familysearch.org**, signed in by hand. The
tool never opens, navigates or closes that tab and never types credentials. FamilySearch's API wants the page's own session as an
`Authorization` header; the in-page script reads it and sends it, and it is never returned, printed or stored.
`FAMILYSEARCH_CLI_PORT` changes the port. `FAMILYSEARCH_CLI_LOCK_FILE` points at a shared lock file (same flock and
epoch-seconds convention as `ANCESTRY_CLI_LOCK_FILE`) so several tools and workers queue; calls are spaced at least 3 seconds apart.
If FamilySearch shows a bot check, the command stops with `familysearch-check-required`: pass it by hand, do not retry.

## Commands
| command | what it does |
| --- | --- |
| `familysearch doctor` | tab check and sign-in check |
| `familysearch search --surname S [--given G] [--birth Y] [--death Y] [--marriage Y] [--years N] [--place P] [--collection C] [--limit N]` | historical record search (`hits[]`: ark, score, collection, principal person with facts, other persons) |
| `familysearch record ARK` | one record: collection, citation, fields (film numbers, record number), persons with facts and roles, relationships |
| `familysearch person PID [--sources]` | a family-tree person: facts, parents, spouses and children with PIDs, attached sources |
| `familysearch film DGS` | catalog entry of a digital film: title, dates, place, image count, the films' contents |
| `familysearch image (--record 1:1:X \| --film DGS --image N \| --ark 3:1:X \| --das TH-...) --out FILE [--crop x,y,w,h] [--max-tiles N]` | save a record image, optionally cropped |

Notes:
- `search` year filters rank results, they do not exclude the others. Check each hit's own date.
- `--image N` counts from 1 on the film. `--das TH-...` is the image id seen in citations. A restricted image stops with
  `familysearch-image-restricted`.
- Images are fetched as deep-zoom tiles and stitched. `--crop` takes full-size pixels, or fractions when all four numbers are 1 or less.
  The tool picks the deepest zoom whose tile count fits `--max-tiles` (default 36, 0.5 s apart), and reports `zoom_level`,
  `reduction` and `saved_size`; crop tighter or raise `--max-tiles` for more detail.
- `--record 1:1:X` uses the image the record links to (`record` lists them as `image_arks`). Many index-only records (for example the
  German baptism indexes) link no image: the command then stops with `familysearch-no-image-link` and names the record's digital film.
- Not covered yet: walking a film's records by place and date.
