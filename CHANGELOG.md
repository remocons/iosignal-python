# Changelog

## 0.2.0 (2026-11-24)

Boho 3: login and encryption move from the old SHA-256 construction to standard Ascon (NIST SP 800-232),
the same as boho 3, IOSignal 10.0.0 (JS), IOSignal Arduino 10.0.0 and the Kotlin client.

- New `iosignal.ascon`: Ascon-AEAD128 and Ascon-CXOF128 in plain Python (CPython and MicroPython).
- `iosignal.boho`: Boho 3 packets (AUTH_REQ, AUTH_RES, ENC, ENC_E2E, ENC_PACK), `derive_key()`,
  `encrypt_session()`/`decrypt_session()` in place of `encrypt_488()`/`decrypt_488()`; `copy_key()` takes 16 bytes.
- The remote REPL (`replproto`) keys its sealer with `derive_key()`; boards and controllers must both run 0.2.0.
- Tests: Ascon known answers, a recorded JS boho 3 session and packs, the shared boho vectors.

Breaking:

- Logging in works only with IOSignal servers 10.0.0 or newer. Against a 10.0.0 server, 0.1.x can still
  connect anonymously but cannot log in; 0.2.0 cannot log in to 9.x servers.
- Keys issued for 9.x servers must be issued and registered again.
- Boards running 0.1.x cannot be updated through the link (`iosignal-repl update`) once the server is 10.0.0:
  install the 0.2.0 package over USB.
- E2E signals and REPL frames sealed by 0.1.x cannot be opened by 0.2.0, and the other way round.

## 0.1.2 (2026-11-24)

REPL key as text; board Wi-Fi send power and 802.11b option, stall reconnect and drop log, merged `$state`
replies, registered names kept; iris and short board examples.

## 0.1.1 (2026-11-23)

Fixes: discover cleanup, plain sends before login, TimeoutError, `ping_server`, NUL, call errors, cong buffering,
shutdown.

## 0.1.0 (2026-11-23)

First release.
