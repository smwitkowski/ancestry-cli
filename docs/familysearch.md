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
| `familysearch page (--ark 3:1:X \| --film DGS --image N)` | every indexed record on one image: names, roles, events, record ARKs (a census page's neighbors, a register page's entries) |
| `familysearch fulltext --q TEXT [--place P] [--from Y] [--to Y] [--type deed\|will\|probate] [--collection C] [--limit N] [--offset N] [--full]` | full-text search of the machine-read handwriting of deeds, wills, probate and court records |
| `familysearch catalog --place P [--subject-id ID] [--years A-B] [--films] [--exact] [--limit N]` | the catalog by place: record types, then titles, then films with DGS numbers |
| `familysearch locate --film DGS [--type baptisms\|marriages\|burials\|births\|deaths] (--date YYYY[-MM[-DD]] \| --years A-B) [--name N] [--probes K]` | which images of a film to look at for a date, by sampling the film's indexed records |
| `familysearch film DGS --sheet --from N --to M --step K --out FILE` | contact sheet of numbered thumbnails (at most 48) |
| `familysearch waypoints --collection C [--waypoint ID] [--query WORDS]` | browse a browsable collection (state, county, district) down to image ARKs |
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
- `fulltext`: each hit is one page image (`ark` = 3:1:...), with the record type, place, date, collection and excerpts around your words.
  Follow up with `familysearch image --ark ARK --out page.jpg [--crop ...]`. `--place` and the year range narrow the search;
  `--type` filters by record type on the pages scanned (up to 4 pages of 50). The handwriting OCR is rough, so try spelling variants
  (Owry, Oury, Qwry). Image numbers inside a film are not part of these results.
- `catalog`: first call with only `--place` lists the record types (subjects) the catalog holds for that place; then call again with
  `--subject-id` for the titles (`--years` keeps overlapping dates, `--films` adds each title's films with DGS and contents, one request
  each). The place is matched as containing the text unless `--exact`; use the catalog's own wording (for example
  `Germany, Bayern, Rockenhausen`) with `--exact` for one place. Feed a DGS number to `film` and `image`.
- `locate` samples about 10 images spread over the film, reads the indexed records on each (type, date, names) and bisects toward the
  target date, up to `--probes` images (about 3 s each). It returns `candidates` (an image range, how it was derived) and the `anchors` it used.
  It only works where the film's images have indexed records; on an unindexed film it says so and the contact sheet (`film --sheet`) is the
  fallback: look at every Kth image, narrow the range, repeat. `--name` stops at the first indexed page that has all the words in a name.
  `sections` is the film's contents text split into parts; FamilySearch does not publish image ranges for them.
- `waypoints` starts at the collection's top level (`--collection C`), then pass a child's `waypoint` id back with `--waypoint`. A leaf
  lists `images` (3:1: ARKs) for `image --ark`.
