# Run your own siphon box

An always-on machine runs `siphon serve`. Your phone and laptop reach it privately through
Tailscale. Finished files go to your own storage (Cloudflare R2 here, any rclone remote works)
and come back to your phone as private links. Nobody else's server holds your files.

The box can be an old laptop, a mini PC, a Raspberry Pi 5 (arm64 is supported) or a small VPS.

## 1. Install siphon

```bash
curl -fsSL https://raw.githubusercontent.com/antharmaya/siphon/master/install.sh | sh
siphon doctor        # what works on this machine and what's missing
```

For pages no extractor knows, siphon drives a real Chrome. On a server without a desktop, install
Google Chrome anyway (it runs headless); `siphon doctor` tells you if it's missing.

## 2. Tailscale: reach it from your devices only

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale set --operator=$USER     # lets siphon run `tailscale serve` without sudo
```

In the Tailscale admin console, under DNS, turn on **MagicDNS** and **HTTPS certificates**. Install
Tailscale on your phone and sign in with the same account.

## 3. R2: storage with no egress fee

1. In the Cloudflare dashboard, create an R2 bucket, e.g. `siphon`.
2. Create an R2 API token with **Object Read & Write**, scoped to that bucket. Note the access key
   ID, the secret, and your account ID.
3. Connect it:

```bash
siphon setup rclone
siphon cloud add r2 --type r2 \
  access_key_id=YOUR_KEY_ID secret_access_key=YOUR_SECRET \
  endpoint=https://YOUR_ACCOUNT_ID.r2.cloudflarestorage.com
siphon cloud default r2:siphon/downloads
```

Your keys stay in rclone's config on the box (`~/.config/rclone/rclone.conf`); siphon never sends
them anywhere. R2 has a free tier (10 GB) and charges nothing for egress, so pulling your files
back to your phone costs nothing.

## 4. Keep it running

A systemd user service, started at boot even when nobody is logged in:

```ini
# ~/.config/systemd/user/siphon.service
[Unit]
Description=siphon serve
After=network-online.target

[Service]
ExecStart=%h/.local/bin/siphon serve --tailscale --no-qr --downloads %h/siphon-downloads
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now siphon
sudo loginctl enable-linger $USER       # keep user services running after logout
journalctl --user -u siphon -f          # its log, including the address
```

`--tailscale` runs `tailscale serve` as a child of siphon, so the address disappears when the
service stops.

## 5. Pair your phone

```bash
siphon serve pair      # a one-time code and a QR code for https://box.your-tailnet.ts.net
```

Open the link on your phone (or scan the QR), then **Add to Home Screen**. On Android you can now
share any link to siphon from other apps; on iPhone, the app's *Share links from other apps*
section walks you through a Shortcut. Choose **r2** under *Save to*. When a download finishes,
**Open** gives you a private link straight from R2 that expires after 24 hours.

## Things that go wrong on a server

- **"Sign in to confirm you're not a bot" from YouTube.** Datacenter IP addresses are often
  challenged. Export a `cookies.txt` from a browser where you're signed in (see yt-dlp's FAQ on
  exporting cookies), copy it to the box and add `--cookies /path/to/cookies.txt` to the
  `ExecStart` line. A box on a home connection rarely hits this.
- **A site works at home but not on the VPS.** Some sites restrict by country or block hosting
  providers. `siphon -v URL` on the box shows the block diagnosis.
- **The disk fills up.** With a cloud destination, the local copy is removed after upload. Downloads
  kept on the box stay in `--downloads`; clear them yourself.
- **Your phone can't reach it.** Check the phone is signed in to the same tailnet and that
  `tailscale status` on the box shows it online.

## Without Tailscale

`siphon serve --cloudflare` gives a public `https://….trycloudflare.com` address with no account.
Pairing still protects it, but the address changes every time siphon restarts, and anyone who
finds it can see the pairing page. Use Tailscale for anything you leave running.
