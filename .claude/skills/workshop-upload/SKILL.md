---
name: workshop-upload
description: Publish or update a Slay the Spire 2 mod to the Steam Workshop using this dashboard's pipeline (workshop_dashboard.py). Use when the user asks to upload / publish / update / release a mod to the Steam Workshop, set its thumbnail / description / visibility, or link an existing Workshop item.
---

# Workshop Upload skill

Drives this dashboard's pipeline to publish or update a *Slay the Spire 2* mod to the
Steam Workshop. It imports `workshop_dashboard.py` and calls its functions directly,
which is more reliable for headless use than streaming the SSE web endpoint.

## ⚠️ Preconditions & safety

- **Uploading creates or updates a REAL Steam Workshop item under the user's account.**
  Confirm with the user before uploading, and default brand-new items to `private` so
  they can verify on Steam before going public.
- **Steam must be running and logged in.** The official uploader talks to the local Steam
  client and cannot run headless. Check `tasklist /FI "IMAGENAME eq steam.exe"`.
- `ModUploader.exe` (+ `steam_api64.dll` + `steam_appid.txt`) must be discoverable — next
  to `workshop_dashboard.py`, via `config.json` `uploader_dir`, or `STS2_UPLOADER_DIR`.
- `pip install pillow` (used to resize thumbnails under Steam's 1 MB preview limit).

## Recipe — run from the repo root

```python
import importlib.util
spec = importlib.util.spec_from_file_location("wd", "workshop_dashboard.py")
wd = importlib.util.module_from_spec(spec); spec.loader.exec_module(wd)

MID = "YourModId"   # must match a mod folder (manifest id) in the game's mods/

# optional: edit the workshop config (this writes workshop.json, which the uploader reads)
cfg = wd.get_mod(MID)["cfg"]
cfg["title"]       = "Your Mod"
cfg["description"] = "[h1]Your Mod[/h1]\n..."   # Steam renders BBCode (see Gotchas)
cfg["visibility"]  = "private"                   # private | public | unlisted | friends_only
cfg["tags"]        = ["QoL"]
cfg["changeNote"]  = "v1.0.0"
wd.save_workshop_cfg(MID, cfg)

# optional: thumbnail (auto-converted to PNG under 1 MB)
wd.set_image_from_path(MID, r"path/to/thumb.png")

# optional: link an EXISTING Workshop item so an update targets it instead of creating a duplicate
wd.write_mod_id(MID, "1234567890")

# upload  (do_build=True runs `dotnet build -c Release` first — needs a csproj mapped in config.json "sources")
m = wd.get_mod(MID)
lines = []
try:
    url = wd.run_pipeline(m, False, lambda s: lines.append(str(s)))
except Exception as e:
    url = None; lines.append(f"EXC {e}")
open("upload.log", "w", encoding="utf-8", errors="replace").write("\n".join(lines))
print("url:", url, "item:", wd.read_mod_id(MID))
```

Wrap that in a single Bash/PowerShell heredoc, then read `upload.log` for the result —
don't stream the uploader's stdout directly (some consoles choke on its encoding).

## Verify the result (authoritative)

- `mod-uploader.log` (next to `ModUploader.exe`):
  - `Successfully uploaded '<title>' ... with id <ID>!` → success.
  - `Using workshop item ID <ID> from mod_id.txt` → it **updated an existing** item (not a new one).
- `.workshop/<ModId>/mod_id.txt` holds the item ID.
  URL: `https://steamcommunity.com/sharedfiles/filedetails/?id=<ID>` (private items are only
  visible to the owner while logged in).

## Gotchas

- **Content** is packaged from the game's installed `mods/<id>/` folder. Runtime junk
  (`*.preset`, `*.log`, `*.tmp`, `mod_id.txt`, …) is auto-excluded — but verify nothing
  user-specific is shipped.
- **Thumbnails must be ≤ 1 MB** (Steam preview limit). `set_image_from_path` and
  `set_image_from_bytes` resize/convert to PNG automatically.
- **Descriptions DO render BBCode** on the Workshop page — write a real structured
  description (`[h1]` title, `[h2]` Features with `[list][*]`, `[b]bold[/b]`, `[url=...]`),
  not a single bare line.
- **New vs update**: `.workshop/<id>/mod_id.txt` links a workspace to its item. If it's
  missing (e.g. a fresh clone), an upload creates a NEW item. Paste the existing ID via
  `write_mod_id` first to avoid a duplicate.
- A successful upload records `uploaded_version.txt`, so the dashboard later flags the mod
  with a ⟳ "update needed" badge when its manifest version changes.
