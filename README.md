# 850 Cedar Lease Generator

A small self-hosted web app that lets a prospective tenant fill out an
intake form, generates a customized Berkeley residential lease (base
lease + 7 addenda + Tenant Protection Ordinance notice) as a PDF, and
sends it to SignWell for e-signature by the tenant(s) and both
landlords. Once fully signed, SignWell emails the executed PDF to all
parties.

## Flow

1. Tenant opens `https://lease850.heaveto.net` and sees the intake form.
2. **Commercial terms (rent, deposit, pet deposit)** are shown as
   read-only — the tenant sees the landlord's set values but can't edit
   them.
3. **Tenant fills in** their name, email, phone, emergency contact,
   move-in/move-out dates, residents list, pet details (if allowed and
   applicable), and renters insurance answer.
4. App generates a personalized lease PDF with SignWell text tags
   embedded invisibly at every signature location.
5. App uploads the PDF to SignWell with all 4 recipients (tenant 1,
   optional tenant 2, both landlords).
6. SignWell emails everyone with a link to sign. Once all signatures
   and initials are captured, SignWell emails the executed PDF to all
   parties.

## Stack

- **FastAPI** (Python) — intake, lease generation, SignWell API
- **Jinja2 + WeasyPrint** — HTML lease template → PDF
- **SignWell** — e-signature with automatic field placement via text tags
- **Docker** — single containerized app
- Your existing **NGINX + Cloudflare DNS** — public HTTPS termination
  and reverse proxy to the app container

## Layout

```
lease-app/
├── Dockerfile              # FastAPI app container
├── docker-compose.yml      # single-service stack
├── .env.example            # copy to .env (or paste into Portainer)
├── nginx/nginx.conf        # reference config if you don't have an NGINX yet
└── app/
    ├── main.py             # routes + config + SignWell client
    ├── requirements.txt
    ├── static/style.css
    └── templates/
        ├── intake.html     # public tenant-facing form
        ├── thanks.html     # post-submission confirmation
        └── lease.html      # the lease itself (Jinja, with SignWell tags)
```

## Configuration (all via env)

See `.env.example` for the full list with comments. The groupings are:

- **SignWell** — API key + test mode toggle.
- **Landlords** — names and emails (both sign everything).
- **Property** — property address + landlord payment address. One
  deployment per property.
- **Commercial terms** — `DEFAULT_RENT`, `DEFAULT_SECURITY_DEPOSIT`,
  `PET_POLICY` (`allowed` or `none`), `DEFAULT_PET_DEPOSIT`. Change these
  in Portainer and restart the stack — the tenant sees the new values on
  their next form load.
- **Admin preview** — `PREVIEW_TOKEN` gates the `/preview` endpoint used
  for template QA.

## How signer routing works

The app hardcodes four signer positions. The Jinja template embeds
SignWell text tags invisibly (white on white) at each signature/date/
initial location:

| ID | Role             | Tag example            |
|----|------------------|------------------------|
| 1  | Primary lessee   | `{{signature:1}}`      |
| 2  | Secondary lessee | `{{signature:2}}`      |
| 3  | Landlord 1       | `{{signature:3}}`      |
| 4  | Landlord 2       | `{{signature:4}}`      |

SignWell parses these tags out of the PDF at upload time and places real
interactive fields at those exact locations. Any change to the lease
template layout automatically works — no drag-and-drop setup in
SignWell's UI.

## Updating the lease text

All lease text lives in `app/templates/lease.html`. Edit, then
`docker compose restart lease-app`. Use the preview endpoint while
iterating:

```
https://lease850.heaveto.net/preview?token=YOUR_TOKEN&primary_lessee_name=Test+Person&num_lessees=2&secondary_lessee_name=Second+Test&has_pets=yes
```

## Things to verify before real use

1. **Have a Berkeley landlord-tenant attorney or BPOA review the lease
   text.** I ported it faithfully from your Google Doc but the RSO
   landscape changes and your duplex may be partially exempt (Golden
   Duplex). The clauses citing the RSO should match your actual
   coverage.
2. **Send a test document to yourself first** in SignWell test mode and
   verify every signature/date/initial field lands where you expect
   across all 25 pages.
3. **Consider the BPOA copyright notice** — the original Google Doc's
   asbestos addendum had a `© BPOA` footer. If you were using that lease
   as a BPOA member, confirm your membership license permits this kind
   of use in your own app.

## What this app does not do

- **Store leases or signed copies.** All storage happens in SignWell's
  dashboard; signed PDFs arrive by email.
- **Authentication for the intake form.** Anyone with the URL can fill
  it out. If you want gating, add Cloudflare Access in front (free for
  up to 50 users) or add a simple shared-secret query param to the
  public URL.
- **Collect screening data** (credit, references, income verification).
  This is just the lease step — run screening separately before handing
  someone the intake URL.
- **Provide legal advice.** Obviously.
