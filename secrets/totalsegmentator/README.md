# TotalSegmentator licence

Put your TotalSegmentator licence key in **this directory**, as a one-line
text file:

```
secrets/totalsegmentator/license.txt
```

It is gitignored and will never be committed. Nothing in this repository ships
a licence — you have to request your own.

## Getting one

Most TotalSegmentator tasks are openly available, but a few are gated behind a
licence key. It is free for non-commercial use:

1. Fill in the form at
   <https://backend.totalsegmentator.com/license-academic/>
   (commercial use: contact jakob.wasserthal@usb.ch).
2. You receive a key by mail, e.g. `aca_XXXXXXXXXXXXXX`.
3. Save just the key as `secrets/totalsegmentator/license.txt`.

If you keep it somewhere else on the host, point `TOTALSEG_LICENSE_DIR` in the
root `.env` at the **directory** containing it (not at the file itself):

```sh
TOTALSEG_LICENSE_DIR=/opt/totalsegmentator-licence
```

## What needs it

| Task | Licence |
|---|---|
| `brain_structures` | **required** |
| `cerebral_bleed`, `brain_aneurysm` | not needed |

The authoritative list of gated tasks is `commercial_models` in the installed
package's `totalsegmentator/map_to_binary.py`; the API reads it from there, so
a newly exposed gated task is picked up automatically.

## How it is used

`API/docker-compose.yml` bind-mounts this directory read-only into the API
container at `/totalseg_license`. For a gated task,
`API/app/total_segmentator_functions.py` reads the key and passes it to the
`TotalSegmentator` CLI as `-l`. The key is masked as `***` in the logged
command line.

TotalSegmentator stores the key in `${TOTALSEG_HOME_DIR}/config.json` and only
validates it online (one request to its licence server) the first time it
sees a new key; later runs with the same key make no request.

## Checking it worked

If the file is missing or empty, a gated job fails before its input is
downloaded, with a message naming `TOTALSEG_LICENSE_DIR` and the licence URL,
rather than TotalSegmentator's own "this task is not openly available"
on stdout. An invalid key fails with TotalSegmentator's
"ERROR: Invalid license number" in the job log.
