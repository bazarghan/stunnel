# stunnel

A fast TCP tunnel between your Iran and Kharej servers. Clients connect to Iran; Iran forwards each connection to Kharej, which sends it to your existing service (such as Xray). Add several Kharej servers or services using separate entry ports.

```text
Clients → Iran server → Kharej server → Xray / other TCP service
```

## Install

Run as **root on both servers** (Linux with systemd; Debian 12+ or Ubuntu 22.04+ recommended, x86_64 / ARM64):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bazarghan/stunnel/main/install.sh)
```

The installer opens the menu and installs automatic startup. It downloads a prebuilt release when available; otherwise, it builds from source automatically (the first build takes a few minutes).

## Set up your tunnel

1. **On Kharej:** choose **Add tunnel → Kharej**. Enter a tunnel port, your service IP/port (usually `127.0.0.1` and your Xray inbound port), and the Iran server's public IP.
2. **On Iran:** choose **Add tunnel → Iran**. Enter the client entry port, Kharej's IP, and the same Kharej tunnel port.
3. Point your clients at **Iran's IP and entry port**, using your existing service credentials and TLS settings.

For example: Iran port `5500` → Kharej port `55000` → Xray port `55601`.

Allow the entry port on Iran and the tunnel port on Kharej in your server/provider firewall. Repeat **Add tunnel** to connect more Kharej servers or ports; each listener needs its own port. Each server is configured separately.

## Optional iperf3 obfuscation

During interactive installation, answer **yes** to **Enable iperf3 obfuscation?** on **both servers**. The default on a fresh installation is **no**; pressing Enter leaves normal TCP forwarding enabled. Reinstalling without selecting a new mode preserves the existing choice.

For an unattended installation, use `--obfuscation yes` (or `--obfuscation no` to disable it):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bazarghan/stunnel/main/install.sh) --obfuscation yes
```

If the latest prebuilt release lacks this feature, the installer automatically builds the current source. You can also add `--source` to request a source build directly. The menu assigns Iran the client role and Kharej the server role. Existing routes are assigned roles from their Iran-IP allowlists unless an explicit role is already configured. The mode applies to all tunnels on that server, so configure matching modes at their other ends.

This custom transport imitates an iperf3 bidirectional TCP test: a random 37-byte cookie, a length-prefixed JSON parameter exchange, control state messages, two data connections, and a final result exchange. Payload scrambling hides recognizable application headers between Iran and Kharej; clients and destination services receive the original bytes. The control/data structure follows the [iperf3 protocol](https://github.com/esnet/iperf/wiki/IperfProtocolStates) and its [bidirectional stream setup](https://github.com/esnet/iperf/blob/master/src/iperf_client_api.c).

This is traffic camouflage, not encryption or authentication: the scrambling seed is public. Keep your service's TLS and credentials. It is not an iperf3 benchmark endpoint, and packet timing, traffic volume and long-lived sessions may still distinguish it from a real benchmark. Classification by traffic inspection is not guaranteed.

Obfuscated transfers use buffered copying. Each active Kharej session holds three incoming sockets (one control and two data), so `max_connections: 512` allows at most 170 fully established sessions there, with fewer available while handshakes are pending. Handshakes have the configured connection timeout; half-closes still allow the other direction to finish.

For manual JSON configuration, set `settings.obfuscation` to `"iperf3"` and add `"obfuscation_role": "client"` to every Iran route or `"obfuscation_role": "server"` to every Kharej route. Omitting `settings.obfuscation` uses normal forwarding. Existing plain configurations continue to work.

## Manage or remove

Open the menu anytime:

```bash
stunnel
```

Use `sudo stunnel` if you are not root. The menu lets you add, edit, list or remove tunnels, view status/logs, start/stop/restart, remove all tunnels, or **completely uninstall** from that server. To remove both ends, run it on both servers.

This tunnel forwards **TCP only** and preserves your application's traffic. Encryption/authentication come from your existing VPN or TLS service. This project is independent of the `stunnel` TLS-wrapper package; its command name must be available before installation.
