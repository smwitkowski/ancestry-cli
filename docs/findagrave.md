# `findagrave`: Find a Grave memorials (read-only)

Public pages over plain HTTP. **No search:** Find a Grave's robots.txt disallows `/memorial/search` and cemetery memorial searches for
automated clients, so the tool refuses to search (`findagrave search` returns `findagrave-search-not-allowed`). Get a memorial id from your
browser, from a citation, or from the family links of another memorial.

- `findagrave memorial ID [--photos]`: name, birth and death (date, place), cemetery (id, name, city, county, state), plot, GPS, bio text,
  inscription, and `family` links (`parents`, `spouses`, `siblings`, `half_siblings`, `children`, each with `memorial_id`, name, born, died).
  `--photos` also lists the memorial's photos (`n`, `photo_id`, `url`).
- `findagrave photo ID --out FILE [--n 1]`: saves the Nth photo.
- Requests identify the tool and are 2 s apart under a lock (`FINDAGRAVE_CLI_LOCK_FILE` shares it).

Environment note: Find a Grave has been seen to answer HTTP 403 to Python builds with an old OpenSSL (3.0) and 200 to a newer one (3.6).
The error then says so (`findagrave-check-required`, with the OpenSSL version). `uv tool install --force --reinstall --python 3.13 .`
installs the tools with a newer Python. The tool does not try to get around a real bot check.
