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

## Reading the result

- A search covers **one region**. It never means the person is not in Poland.
- `reported_total` and `reported_filtered` are the site's own counters. `returned` is the number of rows actually in the answer.
  They have been seen to disagree (31 reported, 8 returned for a Mazowieckie marriage search). Treat a short or empty result as "not
  indexed here, under these spellings", never as proof an act does not exist.
- Marriage (`S`) rows are named: `year`, `act`, `groom` and `bride` (`given`, `surname`, `parents`) and `parish`. Birth and death rows keep
  the raw `cells` because their column layout has not been confirmed.
- Every row adds, when the site gives them: `details` (place or remarks), `archive` (where the books are held), `indexed_by`,
  `scan_url` (the scanned register on metryki.genealodzy.pl), `record_gid` and `parish_id`. The row is an index entry, so it is a lead: read
  the scan before joining it to anyone.
