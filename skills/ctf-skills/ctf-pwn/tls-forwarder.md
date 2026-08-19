# TLS-wrapped Challenge Ports (Target Forwarder)

Some CTF platforms proxy container challenge ports through a TLS "target-forwarder":
your `remote(host, port)` connects, but the forwarder silently drops connections that
never send a TLS ClientHello — you get **0 bytes then EOF** even though the TCP connect
succeeded. The service "menu" never appears.

## How to recognize it

- Plain TCP `remote(host, port)` / `socket.connect` succeeds but every `recv()` returns
  `b''` and the connection closes (`recv 0B`, then EOF), repeatedly, across retries.
- `nc host port` also gets nothing.
- Verify with openssl (note the mandatory `-servername` / SNI):
  ```bash
  timeout 10 openssl s_client -connect host:port -servername host -quiet
  ```
  If the real service banner/menu shows up over TLS, the port is TLS-wrapped.
- The forwarder presents a **self-signed certificate** (`CN=target-forwarder`) and only
  speaks **TLSv1.3** (cipher `TLS_AES_128_GCM_SHA256`).

## Why plain pwntools still fails

- `remote(host, port)` — no TLS at all → forwarder drops you → 0 bytes.
- `remote(host, port, ssl=True)` — pwntools wraps with `SSLContext(PROTOCOL_TLSv1_2)`,
  which pins TLS 1.2. The forwarder only accepts TLS 1.3 → handshake fails → 0 bytes.
  So `ssl=True` alone is **not enough**.

## Fix: TLSv1.3 + SNI + no certificate verification

The certificate is self-signed, so you must disable verification, and you must send SNI
(`server_hostname`). Use a `create_default_context()` (negotiates up to TLS 1.3) with
verification turned off:

```python
from pwn import *
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

r = remote(HOST, PORT, ssl=True, ssl_context=ctx)  # TLSv1.3 + SNI + self-signed OK
r.recvuntil(b"Your choice :")
```

Raw socket equivalent (no pwntools needed):

```python
import socket, ssl
s = socket.create_connection((HOST, PORT), timeout=15)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
s = ctx.wrap_socket(s, server_hostname=HOST)   # server_hostname = SNI, required
```

## Notes

- SNI is mandatory: the forwarder picks the target by SNI. Never omit
  `server_hostname`/`-servername`.
- After the exploit spawns a shell, send commands as usual; the shell takes over the
  TLS stream.
