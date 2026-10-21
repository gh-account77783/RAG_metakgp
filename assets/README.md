# Source assets

`web/` is the single editable source for the current browser reader's CSS and
JavaScript. Keep assets outside `src/`; do not paste them into Python strings.

The pinned setuptools build command in `tools/build_assets.py` copies only the
allowlisted files into `graphmind/web/static/` in a built wheel. Source runs use
this directory explicitly; installed wheels use their packaged resources and
do not depend on the checkout, current working directory, CDN or Node server.

This is the existing reader, not the proposed React frontend. Future frontend
build outputs must use the same explicit packaging boundary. Test-only samples
belong in `tests/fixtures/`; user documents, models, credentials and runtime
stores are not public assets.
