// wf's Cursor host: `node wf-cursor-host.js`, with @cursor/sdk installed beside it (npm ci).
// The host is written in TypeScript (src/), which Node runs as it is, without a build step.
// Node does that from 22.18 on, with its type stripping.

if (!process.features.typescript) {
  process.stderr.write(`Node ${process.version} can't run TypeScript: wf's Cursor host needs Node 22.18 or later\n`);
  process.exit(3);
}
await import("./src/main.ts");
