# VirtRTLab socket API (v1)

Source of truth is the root [README.md](../README.md). This file exists to keep protocol discussions focused.

## Transport

- UNIX domain socket: `/run/virtrtlab.sock`
- Protocol: JSONL (one JSON object per line)

## Envelope

```json
{
  "id": "uuid-or-ci-step-id",
  "op": "inject|profile|query|reset",
  "target": { "bus": "vrtlbus0", "device": "uart0" },
  "ts": { "mode": "immediate|at_monotonic_ns|after_ns", "value": 0 },
  "args": {}
}
```

## Response

```json
{ "id": "…", "ok": true, "result": {} }
```

or

```json
{ "id": "…", "ok": false, "error": { "code": "EINVAL", "message": "…" } }
```
