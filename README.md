# stunnel-relay

A production-oriented transparent TCP relay written in Rust. It preserves the working Iran-initiated transport: every accepted application connection creates a separate TCP connection to its configured destination. It does not prepend tunnel headers, modify TLS, compress traffic, multiplex unrelated flows into one stream, or bond a single flow across transports.

This is a custom relay project, independent of the existing `stunnel` TLS-wrapper package.

## Example deployment

```text
VPN clients → 192.0.2.10:5500 (Iran)
            → 198.51.100.20:55000 (Sweden)
            → 127.0.0.1:55601 (existing Xray inbound)
```

The local test entry `127.0.0.1:5202` on Iran forwards through Sweden port `55100` to its existing local iperf3 listener on `5201`. The example Sweden relay accepts only the configured Iran IP address; this restriction is enforced before allocating connection capacity or connecting to Xray.

The addresses `192.0.2.10` and `198.51.100.20` are documentation examples. Replace them in the example configurations with your own server addresses before deployment. Keep real configurations in the ignored `private/` directory or outside the repository. Point VPN clients at your Iran server and its configured public port, preserving the existing inbound credentials and TLS/REALITY settings.

## Performance and reliability

- Tokio runs asynchronous connections across the available CPU cores.
- On Linux, `copy_mode: "auto"` uses nonblocking `splice` through separate pipes in both directions, avoiding copies through application byte buffers. Other platforms use Tokio's buffered copy implementation. Select `"buffered"` explicitly when desired.
- Bidirectional copying preserves TCP half-close and applies destination backpressure. Errors close both transport sockets; partially transmitted streams are never retried or replayed.
- A global connection limit bounds accepted, queued, connecting, and active sessions. Excess sessions are closed rather than retained in an unbounded queue.
- TCP keepalives and a Linux per-socket user timeout detect unresponsive transport peers without changing global networking settings.
- Connecting to a destination has a configurable timeout. Each new application connection attempts a fresh destination connection, so recovery does not depend on a shared control channel.
- SIGTERM/SIGINT close listeners first, then allow existing connections to drain for up to the configured grace period.
- The systemd service reports readiness only after every configured listener binds successfully, restarts after failures, and starts at boot.
- Logs report aggregate connection counters and bytes from successfully completed sessions every 30 seconds. Individual successful flows are logged only at debug level; payloads and VPN credentials are never logged.

The relay does not add encryption or authentication to the data stream. The existing VPN protocol provides those features, and the Sweden listener additionally restricts source IPs. One application flow uses one transport; independent simultaneous flows can use the link's parallel TCP capacity. Maximum end-to-end speed depends on the network, VPS resources, and VPN workload and must be measured separately.

## Build and checks

The Rust toolchain is pinned in `rust-toolchain.toml`; dependency versions are locked in `Cargo.lock`.

```sh
cargo fmt --check
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo build --release --locked
./target/release/stunnel-relay --check --config configs/iran.json
```

Tests exercise both copy engines with payloads larger than transport buffers, full duplex transfers, delayed replies after half-close, reset cleanup, configuration validation, and source-IP restrictions.

Build the Linux release on Linux. The deployed x86-64 binary is built on Debian 12 and can also run on the Ubuntu 24.04 server. No Rust compiler, Python interpreter, or package installation is required on Sweden to run the resulting executable.

## Install

As root, on each server, from a release/source bundle containing `scripts/` and `deploy/`:

```sh
# Iran
./scripts/install.sh ./target/release/stunnel-relay ./configs/iran.json

# Sweden
./scripts/install.sh ./target/release/stunnel-relay ./configs/sweden.json
```

Installation writes only:

- `/usr/local/bin/stunnel-relay`
- `/etc/stunnel-relay/config.json`
- `/etc/systemd/system/stunnel-relay.service`

The systemd service uses a dynamic unprivileged user, an empty capability set, protected filesystem/kernel settings, and a 65,536 file-descriptor limit. Installation does not change routes, firewall rules, Xray configuration, or existing listeners belonging to other services. The old Python relay must be stopped before starting this relay on its occupied ports.

## Configure and operate

```sh
/usr/local/bin/stunnel-relay --check --config /etc/stunnel-relay/config.json
systemctl status stunnel-relay
journalctl -u stunnel-relay -n 50 --no-pager
systemctl restart stunnel-relay
```

Configuration uses named `routes` with literal IP socket addresses for `listen` and `target`. An omitted or empty `allowed_ips` permits any source. Unknown settings and duplicate listener addresses are rejected. Settings defaults are documented in `src/config.rs`; the production examples limit each process to 512 concurrent sessions.

Validate configuration before restarting. Restarting stops acceptance and drains existing connections for up to 30 seconds, so new client connections may need to retry during this period. No automatic configuration reload is claimed.

To stop and prevent boot startup:

```sh
systemctl disable --now stunnel-relay
```

Stopping closes only this application's listeners and sessions; existing Xray, panel, SSH, firewall, and routing settings remain independently managed.

## Functional test entry

Run one test at a time from your computer:

```sh
ssh iran-server 'iperf3 -c 127.0.0.1 -p 5202 -P 1 -t 20'
ssh iran-server 'iperf3 -c 127.0.0.1 -p 5202 -P 8 -t 20'
ssh iran-server 'iperf3 -c 127.0.0.1 -p 5202 -P 1 -t 20 -R'
ssh iran-server 'iperf3 -c 127.0.0.1 -p 5202 -P 8 -t 20 -R'
```

These commands measure throughput and are separate from the brief, rate-limited functional checks used during deployment. iperf's endpoint retransmission counters do not directly represent the relay's independent inter-server TCP sockets.

Implementation references: [Tokio bidirectional copy](https://docs.rs/tokio/latest/tokio/io/fn.copy_bidirectional_with_sizes.html), [rustix splice](https://docs.rs/rustix/latest/rustix/pipe/fn.splice.html), [socket2 keepalive](https://docs.rs/socket2/latest/socket2/struct.TcpKeepalive.html).
