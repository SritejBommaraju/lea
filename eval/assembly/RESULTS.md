# Assembly check: first measurement (2026-10-25)

`backend/repairos/vision/assembly.py`: one overview call over up to 4 photos, then a full-resolution close look per
claimed problem (at most 3), then up to 2 "could not see it" gaps. Runner: `eval/assembly/run.py`. Labels (source,
licence, fault box, no images): `labels.json`. Per-case answers: `results/2026-10-25.json`.

The images are public (Wikimedia Commons: CC0, public domain, CC BY, CC BY-SA). They were used locally and are
never committed. Each runs as a one-photo scan with no product name. Real scans are the owner's, read only, with the
same 4 stills the pipeline would pick.

| Fault type | n | caught | close-up asked | missed | false alarms |
|---|---|---|---|---|---|
| empty battery bay | 6 | 5 | 0 | 1 (laptop with its battery pack removed: not noticed, only a gap row for the battery connector) | 0 |
| ribbon cable out of its socket | 1 | 1 | 0 | 0 | 0 |
| battery in backwards | 1 | 1 (severity safety) | 0 | 0 | 0 |
| unplugged connector | 0 | no image found with a clear licence | | | |
| missing screw | 0 | no image found | | | |
| cover on but not clipped | 0 | no image found | | | |
| correctly assembled, public (cells in, flex cable seated, two opened laptops) | 4 | | | | 0 |
| real scans: Xbox controller x2, electric trimmer, opened toaster oven | 4 | | | | 0 |

- Recall: 7 of 8 faulty photos. Precision: every issue row (13) was on the labelled fault. Zero false alarms on 8
  negatives. The set is very small: these are not rates to quote, only a sign that the check works and stays quiet.
- Xbox (`scan_75033ad756b7f972`): `no_problems_seen` before and after. One new gap row: "could not check the AA
  battery orientation". The other three real scans keep their verdicts.
- Gap rows: 0-2 per case ("could not check the fan connectors; take a photo of ..."). They never change a
  verdict when other areas were not checked either.
- Duplicates seen: the overview sometimes names a place and a part inside it ("battery bay", "battery contacts") as
  two claims. Two claimed boxes of one photo where one lies at least 60 % inside the other now get one close look
  (`same_place`, added after this run). It drops only the second claim, so no catch above changes.
- Claude calls: 29 in total (overview 16, close looks 13), about 0.45 USD by the spend guard's own counter
  (`repairos.spend`, measured from token counts). A real 4-photo scan cost about 0.03 USD with no claims. With
  claims it cost about 0.03-0.05 USD and took 4-15 s, beside the inspection.
