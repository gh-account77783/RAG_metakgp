# Static reader

The editable React/TypeScript components live in `src/`. Vite emits the shared
reader and session-resume bundle into `../assets/web/`. Production needs the
Python application and these packaged static files; it does not run Node.

Use Node 24.22.1 and the retained npm lock/cache. From this directory:

```text
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run build
```

For a disconnected build, add `--offline --cache <retained-cache>` to `npm ci`.
From the repository root, record and verify the generated assets before building
the Python package:

```text
python -B tools/frontend_assets.py record
python -B tools/frontend_assets.py verify
```

The build record includes source/output hashes, the exact Node version and npm
software notices. Python packaging verifies this record and fails if reader
sources changed without rebuilding. Packaging never invokes npm or downloads
frontend dependencies. `THIRD_PARTY_NOTICES.txt` accompanies the bundled assets.
