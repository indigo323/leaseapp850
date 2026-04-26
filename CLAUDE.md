# lease850 — Claude Code context

## What this is

FastAPI lease-intake + landlord review dashboard + SignWell e-signature app
for 850 Cedar St, Berkeley. Deployed at `https://lease850.heaveto.net`.

## Deployment

| Thing | Value |
|---|---|
| Container | `lease850`, host port 8850 |
| Source | `/home/fezzik/leaseapp850/` |
| GitHub | `git@github.com:indigo323/leaseapp850.git` (main) |
| Portainer stack | `lease850` (ID 66) on `fezzik` env — `192.168.52.25:9000` → Stacks → lease850 |
| Admin | `https://lease850.heaveto.net/admin` |
| PDF preview | `/preview?token=` — token in Portainer stack env |

Rebuild image: `docker build -t lease850:latest /home/fezzik/leaseapp850/`  
Then redeploy: Portainer → Stacks → lease850 → Redeploy  
Env changes: Portainer → Stacks → lease850 → Editor → update env vars → Redeploy  
Logs: `docker logs -f lease850`

## Infrastructure

- Reverse proxy: **Nginx Proxy Manager** at `192.168.52.25:81` — no raw nginx on this host
- TLS: Let's Encrypt via NPM, `ssl_forced=false` (Cloudflare Flexible handles HTTPS enforcement)
- Portainer: `192.168.52.25:9000`

## Critical rules

- **Keep `pydyf==0.10.0` in requirements.txt** — WeasyPrint 62.3 breaks with 0.12.1
- **Never return HTTP 502** — Cloudflare intercepts it and shows its own error page. Use 500 or redirect for SignWell errors.
- **`environment:` section in docker-compose, not `env_file:`** — Portainer pattern
- **`.env` is gitignored** — secrets never go in the repo

## SignWell recipient model

Free plan = max 3 recipients. Decision: Landlord 2 (Paige) signs on paper.
SignWell recipients: Primary Lessee + optional Secondary Lessee + Landlord 1 (Brent).

## Pending items

1. **Resend domain verification** — `heaveto.net` must be verified in Resend dashboard before outbound email works.
2. **SignWell email verification** — account email must be verified at app.signwell.com before approvals work.
3. **Go live** — flip `SIGNWELL_TEST_MODE=false` after smoke test passes.
