# Home AI host (uzu + Open WebUI + SearXNG + Tailscale)

```
users (browser) ──tailnet──> https://<mac>.<tailnet>.ts.net   (tailscale serve)
                                   │
                         Open WebUI :3000 ── SearXNG (internal)
                                   │
                uzu Qwen3.5 9B :8000      uzu LFM2.5 1.2B :8001   (launchd)
```

## Prerequisites (host Mac)
- uzu built: `cd ~/Code/HOSTING_LLM/uzu && cargo build --release -p cli`
- Docker Desktop or OrbStack, set to **start at login**
- Tailscale logged in; in the admin console → DNS enable **MagicDNS** and **HTTPS Certificates**
- For start-after-reboot without anyone logging in: System Settings → Users & Groups → **Automatic login**
  (launchd agents and Docker Desktop run in the user session)

## Install
```bash
./install.sh
```
Then immediately open the printed `https://…ts.net` URL and **sign up first** — that account becomes admin.

## One-time admin clicks
1. **Admin Panel → Settings → Models**: for both models set *Advanced Params → Function Calling = Default*,
   give them friendly names, and make them visible to all users.
2. **Admin Panel → Settings → Interface**: check *Task Model (external)* = LFM.
3. **Admin Panel → Settings → Web Search**: check engine = searxng, URL `http://searxng:8080/search?q=<query>`.
   (If your Open WebUI version ignored the env vars, set it here.)
4. **Admin Panel → Users**: approve people as they sign up.

Users enable search per chat with the 🌐 toggle, upload docs or use Workspace → Knowledge, and turn on
Memory in Settings → Personalization.

## Operate
| Task | Command |
|---|---|
| Model logs | `tail -f ~/Library/Logs/uzu-qwen.log ~/Library/Logs/uzu-lfm.log` |
| Restart a model | `launchctl kickstart -k gui/$(id -u)/local.uzu.qwen` |
| Update Open WebUI | `docker compose pull && docker compose up -d` |
| Update uzu | `cd $UZU_DIR && git pull && cargo build --release -p cli` then kickstart both |
| Stop all | `./uninstall.sh` (keeps chats) / `./uninstall.sh --purge` (deletes chats) |

## Notes
- After rebuilding uzu, re-allow the binary in the macOS firewall if the tailnet can't reach :8000/:8001.
- `BIND_HOST=127.0.0.1 ./install.sh` hides the raw model APIs from the tailnet (Open WebUI still works).
- Env vars in `docker-compose.yml` only seed Open WebUI on first start; later changes go through the Admin UI.
# Local-Inference-Stack
