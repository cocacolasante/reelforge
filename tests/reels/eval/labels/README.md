# Selection eval labels

Hand-labeled ground truth for `./reelforge eval-select`. One JSON file per
asset, named `<asset_id>.json` (the 64-hex content-addressed id — the
directory name under `/data/working/`):

```json
{
  "asset_id": "e0bc0924ac1a27803a686fedb94657fd93ca2fb938f5f430ff92f3706c147286",
  "picks": [
    {"start_sec": 12.0, "end_sec": 47.5, "note": "the big crash"},
    {"start_sec": 130.0, "end_sec": 168.0, "note": "clean carve run"}
  ]
}
```

Each pick is a span you would personally have chosen as a reel. A pick counts
as **recalled** at K when one of the top-K reels in that asset's `reels.json`
overlaps it by at least 50% of the pick's own duration.

`example.json.sample` shows the format; copy it to `<asset_id>.json` with real
times to activate it (only `*.json` files are loaded).

Run:

```bash
./reelforge eval-select                 # labels baked into the cli image mount
./reelforge eval-select --labels /data/eval/labels   # labels elsewhere
```
