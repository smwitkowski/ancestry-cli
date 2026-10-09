# geneteka: Polish parish indexes

`geneteka` searches the public Geneteka index (genealodzy.pl) through its own JSON endpoint. It is read-only and follows the site's
robots.txt: a 120 second gap between requests (a lock shared through `GENETEKA_CLI_LOCK_FILE`), an identifying User-Agent, and a
24 hour cache of every answer (`--refresh` skips it). Because of the gap, the second uncached search in a row waits about two minutes.

```
geneteka regions
geneteka search --region 07mz --type S --surname Murawski --given Antoni --from 1908 --to 1912
geneteka search --region 07mz --type S --surname Murawski --given Antoni --surname2 Raczkowska --given2 Marianna   # paired
```

Options: `--type S|B|D` (marriages, births/baptisms, deaths/burials), `--exact`, `--parents` (also match the parents columns),
`--parish-id N`, `--start N --length N` (the next page start is in `next`).

## What changed after the first version

The first version returned 0 rows for some birth searches while the site reported 1 to 4. The site's grid always sends its sort and search state; without it the filtered rows can come back empty, so those parameters are now always sent (a 1893-1898 Latawiec search went from 0 rows to 3 of 4 reported). The counters still do not match the rows: `--start 3` returned 2 rows, both of which were already in the first page. Results are also fuzzy (names matched through the parents columns). Page with `--start`, de-duplicate on `record_gid`, and never treat the counters as a count of acts.

## Reading the result

- A search covers **one region**. It never means the person is not in Poland.
- `reported_total` and `reported_filtered` are the site's own counters. `returned` is the number of rows actually in the answer.
  They have been seen to disagree (31 reported, 8 returned for a Mazowieckie marriage search). Treat a short or empty result as "not
  indexed here, under these spellings", never as proof an act does not exist.
- Marriage (`S`) rows are named: `year`, `act`, `groom` and `bride` (`given`, `surname`, `parents`) and `parish`. Birth and death rows keep
  the raw `cells`. A births row was seen as year, act, child's given name, surname, father's name, mother's given name and surname, then two place columns; read the site's own column headings before relying on that order.
- Every row adds, when the site gives them: `details` (place or remarks), `archive` (where the books are held), `indexed_by`,
  `scan_url` (the scanned register on metryki.genealodzy.pl), `record_gid` and `parish_id`. The row is an index entry, so it is a lead: read
  the scan before joining it to anyone.
