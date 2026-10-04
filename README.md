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

## Manage or remove

Open the menu anytime:

```bash
stunnel
```

Use `sudo stunnel` if you are not root. The menu lets you add, edit, list or remove tunnels, view status/logs, start/stop/restart, remove all tunnels, or **completely uninstall** from that server. To remove both ends, run it on both servers.

This tunnel forwards **TCP only** and preserves your application's traffic. Encryption/authentication come from your existing VPN or TLS service. This project is independent of the `stunnel` TLS-wrapper package; its command name must be available before installation.
