# The Cursor host

The Cursor host is the program that runs wf's Cursor sessions on Cursor's SDK (`@cursor/sdk`).
wf starts one host for each session, as `node wf-cursor-host.js`, and speaks the wf-session/0 protocol to it over stdin and stdout.
`workflow/harnesses/remote.py` is the other end, and `workflow/harnesses/cursor.py` configures it.

## How it is built and shipped

- There is no build step.
  Node.js 22.18 or later runs the TypeScript in `src/` as it is, with its type stripping.
- `wf-cursor-host.js` is the entry.
  It stops with a clear message on a Node that can't run TypeScript.
- The SDK can't be bundled into one file, so it is installed beside the host, in `node_modules/`.
- The Python wheel ships this folder without `node_modules/`.
  `uv tool install` from git gives the same files.
- The first Cursor session runs `npm ci --omit=dev --ignore-scripts` here, from `package-lock.json`.
  So a machine needs only Node and npm.
- `package-lock.json` pins `@cursor/sdk` 1.0.38 and its packages.
  To change the version, edit `package.json`, run `npm install`, and commit both files.

## Files

- `src/protocol.ts`: the frames, the error classes and the usage object.
- `src/cursor.ts`: one session on the SDK.
  It maps the access levels, the tools, the SDK's messages and its errors.
- `src/models.ts`: how a cursor-agent model id maps to the SDK's catalog.
- `src/main.ts`: hello and welcome, the operations, and the end of the process.
- `protocol/`: the protocol as JSON Schema, and an example of each frame.
  Both test suites read them.

## Checks

```sh
npm ci            # all packages, the ones for the checks too
npm run check     # the TypeScript type check (tsc)
npm test          # the host's own tests (node --test)
```

wf's Python tests run the real host on a fake SDK (`tests/fake_cursor_sdk/`), and they run `npm test` too.
