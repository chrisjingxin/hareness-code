# Agent Plugins 1.0 offline fixtures

These directories are static, de-identified test inputs for ZC-141 Todo 1.
They are not vendored upstream repositories and are never used to start an MCP
process or make a network request.

- `google-spanner-0.3.4/` and `google-alloydb-0.2.0/` preserve the fixed
  upstream shape references recorded in each fixture's `SOURCE.md`.
- `nonfatal-manifest/`, `partial-components/`, `empty-components/`, and
  `malicious-paths/` are local normative edge-case inputs.
