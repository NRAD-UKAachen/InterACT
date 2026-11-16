# FreeSurfer licence

Put your FreeSurfer `license.txt` in **this directory**:

```
secrets/freesurfer/license.txt
```

It is gitignored and will never be committed. Nothing in this repository ships
a licence — you have to request your own.

## Getting one

The licence is free, but it is issued per user and may not be redistributed,
which is why it cannot live in the repo:

1. Fill in the form at
   <https://surfer.nmr.mgh.harvard.edu/registration.html>
2. You receive a short text file by mail (four lines, about 100 bytes).
3. Save it as `secrets/freesurfer/license.txt`.

If you keep it somewhere else on the host, point `FS_LICENSE_DIR` in the root
`.env` at the **directory** containing it (not at the file itself):

```sh
FS_LICENSE_DIR=/opt/freesurfer-licence
```

## What needs it

| Feature | Route / module | How it reads the licence |
|---|---|---|
| FastSurfer (cross-sectional) | `API/app/fastsurfer_functions.py` | this directory is bind-mounted into the sibling `deepmi/fastsurfer` container at `/fs_license`, and passed as `--fs_license` |
| FastSurfer (longitudinal) | `API/app/fastsurfer_long_functions.py` | same mount, plus `FS_LICENSE` in the container environment — the recon-surf scripts read the variable, not the flag |
| SynthSeg | `API/app/synthseg_functions.py` | **not** from here. It runs `mri_synthseg` out of the host FreeSurfer install mounted at `$FREESURFER_HOME`, and picks up that tree's own `license.txt` |
| Hippocampus volumetry | `API/app/hippocampus_functions.py` | FSL FIRST, which needs no FreeSurfer licence — only the label conventions are shared |

So SynthSeg needs a working host FreeSurfer install (`FREESURFER_DIR` in the
root `.env`), while FastSurfer needs the licence file here. Set both up if you
want the full set of surface tools.

## Checking it worked

`API/app/fastsurfer_functions.py`'s `ensure_fs_license()` runs before any
FastSurfer container is launched. If the file is missing you get a message
naming `FS_LICENSE_DIR` and the registration URL, rather than FreeSurfer's own
`license file /opt/freesurfer/.license not found` from three layers down.

To test the failure path deliberately:

```sh
mv secrets/freesurfer/license.txt{,.bak}   # then run a FastSurfer job
```
