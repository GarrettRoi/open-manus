---
name: Replit workspace = open-manus repo
description: Workspace layout and dev-environment quirks for the Open Manus project.
---
- The Replit workspace root IS the `GarrettRoi/open-manus` repo checked out on the `deploy` branch (Railway deploys from it). An untracked `open-manus/` subdir is stale residue from an earlier import — ignore it.
- 15 agent personas = subdirs of `deploy/` containing config.yaml (deploy/shared is not an agent).
- Python interpreter is 3.13 but `.pythonlibs` may still hold a stale 3.12 site-packages tree; if imports fail despite packages "existing", check `ls .pythonlibs/lib/` and reinstall with `uv pip install --python .pythonlibs/bin/python3`.
- Node isn't installed by default (`python-base` module only); install the nodejs module before `npm` work in `web/`.
