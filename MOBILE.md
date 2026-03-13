# Mobile access — SSH + Termius setup

Full orch functionality from your iPhone or iPad, with zero password typing.

---

## SSH key setup (do this once on your Mac)

```bash
# Generate a dedicated key for mobile access
ssh-keygen -t ed25519 -C "orch-mobile" -f ~/.ssh/orch_mobile

# Add public key to authorized_keys
cat ~/.ssh/orch_mobile.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# Enable SSH on your Mac
# System Settings → General → Sharing → Remote Login → On
# Set "Allow access for: Only these users" and add yourself
```

Find your Mac's local IP (for home network access):
```bash
ipconfig getifaddr en0
```

---

## Termius setup (iPhone / iPad)

1. Install Termius from the App Store (free tier works, Pro adds sync)
2. **Keychain** → Add Key → Import the private key `~/.ssh/orch_mobile`
   - AirDrop it to your phone, or paste the contents from:
     `cat ~/.ssh/orch_mobile`
3. **Hosts** → New Host:
   - Alias: `mac-home`
   - Hostname: your Mac's local IP (e.g. `192.168.1.42`)
   - Port: `22`
   - Username: your Mac username (`whoami` to check)
   - Key: select `orch_mobile`
4. Tap connect — no password prompt, drops straight into your shell

Once connected:
```bash
orch          # full TUI — works great on iPad, usable on phone landscape
orch plan     # day plan text output — perfect for phone
orch stage cacao-dna mvp "Core loop working"   # update from phone
```

---

## Cloudflare tunnel (access from anywhere, not just home network)

This gives you a stable public HTTPS URL that proxies to your Mac's local
bridge server. Works through any firewall, no static IP needed.

```bash
# Install cloudflared
brew install cloudflared

# Authenticate (one-time — opens browser)
cloudflared tunnel login

# Create a named tunnel
cloudflared tunnel create orch

# Add a DNS route (replace with your domain)
cloudflared tunnel route dns orch orch.yourdomain.com

# Start the tunnel pointing at the bridge port
cloudflared tunnel run --url http://localhost:7777 orch
```

To start automatically on login:
```bash
cloudflared service install
```

Then in Termius you can also add a second host:
- Hostname: your home IP or a dynamic DNS hostname
- Same key as above
- This works when you're away from home

---

## SSH tunnel for the bridge (alternative to Cloudflare)

If you don't want Cloudflare, Termius supports SSH port forwarding:

In Termius → Host → Port Forwarding → Add:
- Type: Local
- Local port: `7777`
- Remote host: `localhost`
- Remote port: `7777`

Then browse to `http://localhost:7777` on your phone while connected.

---

## What you can do from mobile

| Action | How |
|--------|-----|
| Check all project statuses | `orch` (TUI) or browser → `orch.yourdomain.com` |
| See day plan | `orch plan` |
| Add a todo | Browser UI or `orch stage` CLI |
| Advance a project stage | `orch stage <project> <stage> "note"` |
| Send a task to Claude | Browser UI → Todos → bottom input |
| Tail logs | `orch logs <project>` |
| Open a Claude session | SSH in, `cd ~/Sites/<project> && claude` |

---

## Recommended Termius settings for orch

- **Font**: FiraCode Nerd Font or MesloLGS NF (same as your iTerm2 profile)
- **Font size**: 12pt on iPhone, 14pt on iPad
- **Color scheme**: Dark (matches the orch iTerm2 profile)
- **Keyboard**: Enable extended row (Ctrl, Esc, Tab) — needed for TUI navigation
- **Keep alive**: 60s (prevents SSH timeout while reading the TUI)

In Termius settings → Appearance → Extended keyboard row, enable:
Escape, Tab, Ctrl, Arrow keys — these are all used by the orch TUI.
