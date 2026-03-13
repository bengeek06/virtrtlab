# Userspace tooling

`virtrtlabctl` is a tiny helper to talk to the VirtRTLab UNIX socket.

## Run

```bash
python3 ./virtrtlabctl.py --help
```

## Send a raw JSONL message

```bash
python3 ./virtrtlabctl.py send '{"id":"1","op":"query","target":{"bus":"vrtlbus0"},"ts":{"mode":"immediate","value":0},"args":{"kind":"devices"}}'
```

By default the socket path is `/run/virtrtlab.sock`. Override with `--socket`.
