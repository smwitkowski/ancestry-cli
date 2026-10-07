# `newspapers`: free newspaper archives

A third command in the package, read-only, over plain HTTP (no browser). IDs look like `loc:sn87075008/1935-02-14/ed-1/seq-7`.

| provider | site | status |
| --- | --- | --- |
| `loc` | Chronicling America at loc.gov (1770-1963) | works |
| `pa` | Pennsylvania Newspaper Archive (panewsarchive.psu.edu) | works |
| `nys` | nyshistoricnewspapers.org | behind a Cloudflare bot check; not supported |
| `brooklyn` | bklyn.newspapers.com (Brooklyn Daily Eagle) | behind a Cloudflare bot check; not supported |

The tool does not try to pass a bot check. `search --provider all` skips the blocked ones and lists them under `skipped`.
Requests identify the tool, are 1.5 s apart under a lock (`NEWSPAPERS_CLI_LOCK_FILE` shares it), and back off once on 429.

- `newspapers doctor`
- `newspapers search --q TEXT [--provider loc|pa|all] [--from Y[-M-D]] [--to Y[-M-D]] [--state NAME] [--paper LCCN] [--limit N] [--page N]`
  Hits have `id`, title, date, state, url and (pa) a snippet. loc gives no snippet: use `page --find`.
- `newspapers page ID [--find WORDS] [--context N] [--max-chars N]` the page's OCR text; with `--find`, excerpts around each match
  (best for obituaries). OCR is imperfect: try variant spellings. There is no article-level split.
- `newspapers image ID --out FILE [--crop x,y,w,h] [--scale PCT]` saves the page, or a crop of it (pixels of the full-size image, or
  fractions when all four are 1 or less). `loc` crops server-side, so a small crop is cheap.
