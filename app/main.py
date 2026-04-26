"""
Lease workflow app.

Flow:
    /              intake form (public)
    POST /submit   saves JSON, emails landlord — NO SignWell yet

    /admin         login
    /admin/        dashboard — lists submissions by status
    /admin/submission/{id}           detail view
    POST  .../approve                generates lease, sends to SignWell, emails tenant
    POST  .../request-changes        emails tenant a fix-link
    POST  .../decline                marks closed
    GET   .../lease.pdf              preview PDF (landlord-only)

    /resubmit/{id}?t=TOKEN           tenant's fix-link (pre-filled form)
    POST /resubmit/{id}              tenant updates the submission

    /healthz                         health check
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (HTMLResponse, RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from weasyprint import HTML

from auth import (SESSION_COOKIE, SESSION_MAX_AGE, is_authenticated,
                  issue_session_cookie, make_fix_token, verify_fix_token,
                  verify_password)
from email_client import send as send_email
import storage


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
ASSETS_DIR = BASE_DIR / "assets"

# Baked-in PDF attachments — served statically and/or merged into the
# signing document. Paths are checked at startup; missing files log a
# warning but don't crash the app.
LEAD_PAINT_BROCHURE = ASSETS_DIR / "lead_paint_brochure.pdf"
MOVE_IN_CHECKLIST = ASSETS_DIR / "move_in_checklist.pdf"

SIGNWELL_API_KEY = os.environ.get("SIGNWELL_API_KEY", "")
SIGNWELL_API_BASE = "https://www.signwell.com/api/v1"
SIGNWELL_TEST_MODE = os.environ.get("SIGNWELL_TEST_MODE", "true").lower() == "true"

LANDLORD1_NAME = os.environ.get("LANDLORD1_NAME", "Brent Buck")
LANDLORD1_EMAIL = os.environ.get("LANDLORD1_EMAIL", "brent@example.com")
LANDLORD2_NAME = os.environ.get("LANDLORD2_NAME", "Paige Buck")
LANDLORD2_EMAIL = os.environ.get("LANDLORD2_EMAIL", "paige@example.com")

PROPERTY_ADDRESS = os.environ.get(
    "PROPERTY_ADDRESS", "850 Cedar Street, Berkeley, CA 94710")
LANDLORD_PAYMENT_ADDRESS = os.environ.get(
    "LANDLORD_PAYMENT_ADDRESS", "852 Cedar St., Berkeley, CA 94710")

DEFAULT_RENT = os.environ.get("DEFAULT_RENT", "3000")
DEFAULT_SECURITY_DEPOSIT = os.environ.get("DEFAULT_SECURITY_DEPOSIT", "3000")
PET_POLICY = os.environ.get("PET_POLICY", "allowed").lower()
DEFAULT_PET_DEPOSIT = os.environ.get("DEFAULT_PET_DEPOSIT", "500")

ADMIN_NOTIFY_EMAIL = os.environ.get("ADMIN_NOTIFY_EMAIL", LANDLORD1_EMAIL)
# Reply-To address used on tenant-facing emails so replies reach a real
# inbox you monitor (the SMTP_FROM address is typically send-only).
LANDLORD_REPLY_EMAIL = os.environ.get("LANDLORD_REPLY_EMAIL", LANDLORD1_EMAIL)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL",
                                 "https://lease850.heaveto.net").rstrip("/")
# The URL embedded in the lease so the signed PDF contains a live link
# tenants can click to download the blank Move-In/Move-Out Checklist.
# Derives from PUBLIC_BASE_URL by default; override only if needed.
CHECKLIST_URL = os.environ.get("CHECKLIST_URL", f"{PUBLIC_BASE_URL}/checklist.pdf")

PREVIEW_TOKEN = os.environ.get("PREVIEW_TOKEN", "")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("lease-app")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="850 Cedar Lease Generator")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

pdf_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_residents(form: dict[str, Any]) -> list[dict[str, str]]:
    residents = []
    for i in range(1, 6):
        name = (form.get(f"resident_name_{i}") or "").strip()
        dob = (form.get(f"resident_dob_{i}") or "").strip()
        if name:
            residents.append({"name": name, "dob": dob or "—"})
    return residents


def _money(raw: str) -> str:
    if not raw:
        return "$0"
    try:
        cleaned = str(raw).replace(",", "").replace("$", "").strip()
        val = float(cleaned)
        if val == int(val):
            return f"${int(val):,}"
        return f"${val:,.2f}"
    except ValueError:
        return f"${raw}"


def _build_context(form: dict[str, Any]) -> dict[str, Any]:
    residents = _parse_residents(form)
    num_lessees = int(form.get("num_lessees") or "1")
    has_pets = (form.get("has_pets") or "no").lower() == "yes"
    if PET_POLICY == "none":
        has_pets = False

    return {
        "primary_lessee_name": (form.get("primary_lessee_name") or "").strip(),
        "primary_lessee_email": (form.get("primary_lessee_email") or "").strip(),
        "primary_lessee_phone": (form.get("primary_lessee_phone") or "").strip(),
        "num_lessees": num_lessees,
        "secondary_lessee_name": (form.get("secondary_lessee_name") or "").strip(),
        "secondary_lessee_email": (form.get("secondary_lessee_email") or "").strip(),
        "secondary_lessee_phone": (form.get("secondary_lessee_phone") or "").strip(),
        "emergency_contact_name": (form.get("emergency_contact_name") or "").strip(),
        "emergency_contact_phone": (form.get("emergency_contact_phone") or "").strip(),
        "landlord1_name": LANDLORD1_NAME,
        "landlord2_name": LANDLORD2_NAME,
        "residents": residents,
        "num_residents": len(residents),
        "lease_date": date.today().isoformat(),
        "start_date": form.get("start_date") or "",
        "end_date": form.get("end_date") or "",
        "rent": _money(DEFAULT_RENT),
        "security_deposit": _money(DEFAULT_SECURITY_DEPOSIT),
        "has_pets": has_pets,
        "num_pets": form.get("num_pets") or "",
        "pet_description": (form.get("pet_description") or "").strip(),
        "pet_deposit": _money(DEFAULT_PET_DEPOSIT) if has_pets else "",
        "renters_insurance": (form.get("renters_insurance") or "No").strip(),
        "property_address": PROPERTY_ADDRESS,
        "landlord_payment_address": LANDLORD_PAYMENT_ADDRESS,
        "checklist_url": CHECKLIST_URL,
    }


def _render_lease_pdf(ctx: dict[str, Any]) -> bytes:
    template = pdf_env.get_template("lease.html")
    html = template.render(**ctx)
    return HTML(string=html, base_url=str(TEMPLATES_DIR)).write_pdf()


def _render_signing_bundle(ctx: dict[str, Any]) -> bytes:
    """
    Generate the full document that gets sent to SignWell: the lease PDF
    followed by the EPA lead paint brochure (federal disclosure requirement
    for pre-1978 housing). Tenant's initials on the Lead-Based Paint
    Disclosure page acknowledge receipt.

    If the brochure asset is missing (shouldn't happen in a proper build),
    returns the lease alone and logs a warning.
    """
    lease_pdf = _render_lease_pdf(ctx)

    if not LEAD_PAINT_BROCHURE.exists():
        log.warning("Lead paint brochure missing at %s; sending lease only",
                    LEAD_PAINT_BROCHURE)
        return lease_pdf

    from io import BytesIO
    from pypdf import PdfWriter, PdfReader

    writer = PdfWriter()
    # Add lease pages
    lease_reader = PdfReader(BytesIO(lease_pdf))
    for page in lease_reader.pages:
        writer.add_page(page)
    # Add brochure pages
    brochure_reader = PdfReader(str(LEAD_PAINT_BROCHURE))
    for page in brochure_reader.pages:
        writer.add_page(page)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _form_dict_from_submission(record: dict[str, Any]) -> dict[str, Any]:
    """The form_data on a submission already IS a form dict; this is just a
    readability alias."""
    return record["form_data"]


REQUIRED_FIELDS = [
    "primary_lessee_name", "primary_lessee_email", "primary_lessee_phone",
    "start_date", "end_date",
    "emergency_contact_name", "emergency_contact_phone",
    "resident_name_1", "resident_dob_1",
]


def _validate(form: dict[str, Any]) -> list[str]:
    return [k for k in REQUIRED_FIELDS if not form.get(k)]


# ---------------------------------------------------------------------------
# SignWell
# ---------------------------------------------------------------------------


async def _send_to_signwell(pdf_bytes: bytes, ctx: dict[str, Any]) -> dict:
    """Upload PDF to SignWell with text_tags=True. Raises on HTTP errors."""
    recipients = [{
        "id": "1", "placeholder_name": "Primary Lessee",
        "name": ctx["primary_lessee_name"], "email": ctx["primary_lessee_email"],
    }]
    if ctx["num_lessees"] == 2 and ctx["secondary_lessee_email"]:
        recipients.append({
            "id": "2", "placeholder_name": "Secondary Lessee",
            "name": ctx["secondary_lessee_name"],
            "email": ctx["secondary_lessee_email"],
        })
    recipients.append({
        "id": "3", "placeholder_name": "Landlord 1",
        "name": LANDLORD1_NAME, "email": LANDLORD1_EMAIL,
    })
    # Note: LANDLORD2 is named throughout the lease text but does NOT receive
    # a SignWell signing request. SignWell's free plan caps recipients at 3.
    # If the second landlord needs to sign electronically, they can be added
    # to the executed PDF separately, or both landlords can countersign on
    # paper.

    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")

    context_lines = [
        f"Primary tenant: {ctx['primary_lessee_name']} "
        f"({ctx['primary_lessee_email']}, {ctx['primary_lessee_phone']})",
    ]
    if ctx["num_lessees"] == 2 and ctx["secondary_lessee_name"]:
        context_lines.append(
            f"Second tenant: {ctx['secondary_lessee_name']} "
            f"({ctx['secondary_lessee_email']}, {ctx['secondary_lessee_phone']})"
        )
    context_lines.append(
        f"Emergency contact: {ctx['emergency_contact_name']} "
        f"({ctx['emergency_contact_phone']})"
    )
    context_lines.append(f"Move-in: {ctx['start_date']}  End: {ctx['end_date']}")

    payload = {
        "test_mode": SIGNWELL_TEST_MODE,
        "name": f"Lease — {ctx['primary_lessee_name']} — {PROPERTY_ADDRESS}",
        "subject": f"Please sign your lease for {PROPERTY_ADDRESS}",
        "message": (
            "Please review and sign the attached lease agreement. A fully "
            "executed copy will be emailed to all parties once signing is "
            "complete.\n\n"
            "--- Intake details ---\n" + "\n".join(context_lines)
        ),
        "files": [{"name": "lease.pdf", "file_base64": pdf_b64}],
        "recipients": recipients,
        "text_tags": True,
        "reminders": True,
        "allow_decline": True,
    }

    headers = {"X-Api-Key": SIGNWELL_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(f"{SIGNWELL_API_BASE}/documents/",
                              json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
        log.info("SignWell document created: id=%s status=%s test_mode=%s",
                 data.get("id"), data.get("status"), SIGNWELL_TEST_MODE)
        return data


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def require_admin(request: Request) -> None:
    if not is_authenticated(request):
        # Browser-friendly: redirect to login, preserve destination
        raise HTTPException(
            status_code=307,
            headers={"Location": f"/admin?next={request.url.path}"},
        )


# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def health():
    return {"ok": True}


@app.get("/checklist.pdf")
async def move_in_checklist():
    """
    Serve the blank Move-In / Move-Out Checklist. Public link referenced
    from the lease body. Tenant downloads, fills out during walk-through
    with landlord, both parties sign.
    """
    if not MOVE_IN_CHECKLIST.exists():
        raise HTTPException(status_code=404, detail="Checklist asset missing")
    return Response(
        content=MOVE_IN_CHECKLIST.read_bytes(),
        media_type="application/pdf",
        headers={"Content-Disposition":
                 'inline; filename="850-cedar-move-in-checklist.pdf"'},
    )


def _intake_context(*, existing: dict[str, Any] | None = None,
                    fix_note: str | None = None, submission_id: str | None = None,
                    token: str | None = None) -> dict[str, Any]:
    return {
        "property_address": PROPERTY_ADDRESS,
        "default_rent": _money(DEFAULT_RENT),
        "default_security_deposit": _money(DEFAULT_SECURITY_DEPOSIT),
        "default_pet_deposit": _money(DEFAULT_PET_DEPOSIT),
        "pet_policy": PET_POLICY,
        "existing": existing or {},
        "fix_note": fix_note,
        "submission_id": submission_id,
        "token": token,
    }


@app.get("/", response_class=HTMLResponse)
async def intake_form(request: Request):
    return templates.TemplateResponse(request, "intake.html", _intake_context())


@app.post("/submit")
async def submit(request: Request):
    form = dict(await request.form())
    missing = _validate(form)
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"Missing required fields: {', '.join(missing)}")

    record = storage.create(form)
    sid = record["id"]
    ctx = _build_context(form)

    log.info("SUBMISSION %s | primary=%s <%s> | move-in=%s",
             sid, ctx["primary_lessee_name"], ctx["primary_lessee_email"],
             ctx["start_date"])

    # Email the landlord a review link.
    review_url = f"{PUBLIC_BASE_URL}/admin/submission/{sid}"
    landlord_body = (
        f"A new lease application was submitted.\n\n"
        f"Primary tenant: {ctx['primary_lessee_name']} <{ctx['primary_lessee_email']}>\n"
        f"Phone: {ctx['primary_lessee_phone']}\n"
        f"Move-in: {ctx['start_date']}  End: {ctx['end_date']}\n"
        f"Emergency contact: {ctx['emergency_contact_name']} "
        f"({ctx['emergency_contact_phone']})\n\n"
        f"Review and approve: {review_url}\n"
    )
    await send_email(
        to=ADMIN_NOTIFY_EMAIL,
        subject=f"[Lease] New application from {ctx['primary_lessee_name']}",
        body=landlord_body,
        reply_to=ctx["primary_lessee_email"],
    )

    return RedirectResponse(url="/thanks", status_code=303)


@app.get("/thanks", response_class=HTMLResponse)
async def thanks(request: Request):
    return templates.TemplateResponse(request, "thanks.html", {})


# ---------------------------------------------------------------------------
# Tenant resubmit flow (fix-link)
# ---------------------------------------------------------------------------


@app.get("/resubmit/{sid}", response_class=HTMLResponse)
async def resubmit_form(request: Request, sid: str, t: str = Query(...)):
    if not verify_fix_token(t, sid):
        raise HTTPException(status_code=403, detail="Link expired or invalid")
    record = storage.load(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    if record["status"] != "changes_requested":
        raise HTTPException(
            status_code=409,
            detail="This application is not awaiting corrections.",
        )
    return templates.TemplateResponse(request, "intake.html", _intake_context(
        existing=record["form_data"],
        fix_note=record.get("tenant_fix_note"),
        submission_id=sid,
        token=t,
    ))


@app.post("/resubmit/{sid}")
async def resubmit_post(request: Request, sid: str, t: str = Query(...)):
    if not verify_fix_token(t, sid):
        raise HTTPException(status_code=403, detail="Link expired or invalid")
    record = storage.load(sid)
    if record is None:
        raise HTTPException(status_code=404, detail="Submission not found")
    if record["status"] != "changes_requested":
        raise HTTPException(status_code=409,
                            detail="This application is not awaiting corrections.")

    form = dict(await request.form())
    missing = _validate(form)
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"Missing required fields: {', '.join(missing)}")

    storage.update_status(sid, status="pending_review",
                          event="resubmitted", form_data=form)

    ctx = _build_context(form)
    review_url = f"{PUBLIC_BASE_URL}/admin/submission/{sid}"
    await send_email(
        to=ADMIN_NOTIFY_EMAIL,
        subject=f"[Lease] Updated application from {ctx['primary_lessee_name']}",
        body=(f"The applicant resubmitted with corrections.\n\n"
              f"Review: {review_url}\n"),
        reply_to=ctx["primary_lessee_email"],
    )

    return RedirectResponse(url="/thanks", status_code=303)


# ---------------------------------------------------------------------------
# Admin — login
# ---------------------------------------------------------------------------


@app.get("/admin", response_class=HTMLResponse)
async def admin_login_page(request: Request, next: str = "/admin/"):
    if is_authenticated(request):
        return RedirectResponse(url=next, status_code=303)
    return templates.TemplateResponse(request, "admin_login.html", {
        "next": next, "error": None,
    })


@app.post("/admin/login")
async def admin_login(request: Request,
                      password: str = Form(...),
                      next: str = Form("/admin/")):
    if not verify_password(password):
        return templates.TemplateResponse(request, "admin_login.html", {
            "next": next,
            "error": "Incorrect password.",
        }, status_code=401)

    cookie = issue_session_cookie()
    resp = RedirectResponse(url=next, status_code=303)
    # Secure flag on HTTPS only. In production the app sits behind NGINX
    # with X-Forwarded-Proto=https; we trust that header because uvicorn is
    # launched with --forwarded-allow-ips=*.
    is_https = request.url.scheme == "https" or \
               request.headers.get("x-forwarded-proto") == "https"
    resp.set_cookie(
        SESSION_COOKIE, cookie,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=is_https,
        samesite="lax",
    )
    return resp


@app.get("/admin/logout")
async def admin_logout():
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---------------------------------------------------------------------------
# Admin — dashboard
# ---------------------------------------------------------------------------


@app.get("/admin/", response_class=HTMLResponse,
         dependencies=[Depends(require_admin)])
async def admin_dashboard(request: Request):
    records = storage.list_all()
    # Group by status for easy scanning
    buckets = {
        "pending_review": [],
        "changes_requested": [],
        "approved": [],
        "declined": [],
    }
    for r in records:
        buckets.setdefault(r["status"], []).append(r)
    return templates.TemplateResponse(request, "admin_dashboard.html", {
        "buckets": buckets,
        "total": len(records),
    })


@app.get("/admin/submission/{sid}", response_class=HTMLResponse,
         dependencies=[Depends(require_admin)])
async def admin_submission(request: Request, sid: str):
    record = storage.load(sid)
    if record is None:
        raise HTTPException(status_code=404)
    ctx = _build_context(record["form_data"])
    return templates.TemplateResponse(request, "admin_submission.html", {
        "record": record,
        "ctx": ctx,
    })


@app.get("/admin/submission/{sid}/lease.pdf",
         dependencies=[Depends(require_admin)])
async def admin_submission_pdf(sid: str):
    record = storage.load(sid)
    if record is None:
        raise HTTPException(status_code=404)
    ctx = _build_context(record["form_data"])
    # Return the full signing bundle (lease + brochure) — this is exactly
    # what will be sent to SignWell on approval.
    pdf = _render_signing_bundle(ctx)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.post("/admin/submission/{sid}/approve",
          dependencies=[Depends(require_admin)])
async def admin_approve(sid: str):
    record = storage.load(sid)
    if record is None:
        raise HTTPException(status_code=404)
    if record["status"] not in ("pending_review", "changes_requested"):
        raise HTTPException(status_code=409,
                            detail=f"Cannot approve from status {record['status']}")

    ctx = _build_context(record["form_data"])
    bundle_pdf = _render_signing_bundle(ctx)

    if not SIGNWELL_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="SIGNWELL_API_KEY not configured. Can't send for signature.",
        )

    try:
        data = await _send_to_signwell(bundle_pdf, ctx)
    except httpx.HTTPStatusError as e:
        log.error("SignWell rejected document for %s: %s %s",
                  sid, e.response.status_code, e.response.text)
        msg = f"SignWell error {e.response.status_code}: {e.response.text[:300]}"
        return RedirectResponse(
            url=f"/admin/submission/{sid}?error={msg}", status_code=303)
    except httpx.HTTPError as e:
        log.error("SignWell transport error for %s: %s", sid, e)
        return RedirectResponse(
            url=f"/admin/submission/{sid}?error=Could+not+reach+SignWell",
            status_code=303)

    storage.update_status(
        sid, status="approved", event="approved",
        signwell_document_id=data.get("id"),
    )

    # Notify tenant the lease is on the way
    await send_email(
        to=ctx["primary_lessee_email"],
        subject=f"Your lease for {PROPERTY_ADDRESS} is on its way",
        body=(
            f"Hi {ctx['primary_lessee_name']},\n\n"
            f"Your lease application has been approved. You'll receive a "
            f"separate email from SignWell within a few minutes with a link "
            f"to sign. Please check your spam folder if you don't see it.\n\n"
            f"Once all parties sign, SignWell will email everyone a copy of "
            f"the fully-executed lease.\n\n"
            f"Thanks,\n{LANDLORD1_NAME}\n"
        ),
        reply_to=LANDLORD_REPLY_EMAIL,
    )

    return RedirectResponse(url=f"/admin/submission/{sid}", status_code=303)


@app.post("/admin/submission/{sid}/request-changes",
          dependencies=[Depends(require_admin)])
async def admin_request_changes(sid: str, note: str = Form(...)):
    record = storage.load(sid)
    if record is None:
        raise HTTPException(status_code=404)
    if record["status"] not in ("pending_review", "changes_requested"):
        raise HTTPException(status_code=409,
                            detail=f"Cannot request changes from status {record['status']}")

    note = note.strip()
    if not note:
        raise HTTPException(status_code=400,
                            detail="A note explaining what needs fixing is required.")

    token = make_fix_token(sid)
    fix_url = f"{PUBLIC_BASE_URL}/resubmit/{sid}?t={token}"

    storage.update_status(
        sid, status="changes_requested", event="changes_requested",
        note=note, tenant_fix_note=note,
    )

    form_data = record["form_data"]
    tenant_email = (form_data.get("primary_lessee_email") or "").strip()
    tenant_name = (form_data.get("primary_lessee_name") or "").strip()

    await send_email(
        to=tenant_email,
        subject=f"Please update your lease application for {PROPERTY_ADDRESS}",
        body=(
            f"Hi {tenant_name},\n\n"
            f"We need a small correction on your lease application before "
            f"we can generate your lease:\n\n"
            f"  {note}\n\n"
            f"Please update your application here (link expires in 7 days):\n"
            f"  {fix_url}\n\n"
            f"Your previous answers are pre-filled — just fix the item above "
            f"and resubmit.\n\n"
            f"Thanks,\n{LANDLORD1_NAME}\n"
        ),
        reply_to=LANDLORD_REPLY_EMAIL,
    )

    return RedirectResponse(url=f"/admin/submission/{sid}", status_code=303)


@app.post("/admin/submission/{sid}/decline",
          dependencies=[Depends(require_admin)])
async def admin_decline(sid: str, reason: str = Form("")):
    record = storage.load(sid)
    if record is None:
        raise HTTPException(status_code=404)
    storage.update_status(sid, status="declined",
                          event="declined", note=reason or None)
    return RedirectResponse(url=f"/admin/submission/{sid}", status_code=303)


@app.get("/admin/submission/{sid}/edit", response_class=HTMLResponse,
         dependencies=[Depends(require_admin)])
async def admin_edit_form(request: Request, sid: str):
    """Load the intake form pre-filled with the submission's current data,
    in admin-edit mode (no read-only commercial terms, status choice shown)."""
    record = storage.load(sid)
    if record is None:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "admin_edit.html", {
        "record": record,
        "existing": record["form_data"],
        "property_address": PROPERTY_ADDRESS,
        "default_rent": _money(DEFAULT_RENT),
        "default_security_deposit": _money(DEFAULT_SECURITY_DEPOSIT),
        "default_pet_deposit": _money(DEFAULT_PET_DEPOSIT),
        "pet_policy": PET_POLICY,
    }, headers={"Cache-Control": "no-store"})


@app.post("/admin/submission/{sid}/edit",
          dependencies=[Depends(require_admin)])
async def admin_edit_save(request: Request, sid: str):
    """Save admin edits to a submission. Optionally changes status."""
    record = storage.load(sid)
    if record is None:
        raise HTTPException(status_code=404)

    form = dict(await request.form())
    new_status = form.pop("_new_status", "").strip() or record["status"]

    # Validate status value
    valid_statuses = {"pending_review", "changes_requested", "approved", "declined"}
    if new_status not in valid_statuses:
        raise HTTPException(status_code=400,
                            detail=f"Invalid status: {new_status}")

    # Minimal required field check
    missing = _validate(form)
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"Missing required fields: {', '.join(missing)}")

    # Always keep resident_name_1/2 in sync with lessee names.
    # The JS in admin_edit.html does this live while typing; this is the
    # server-side fallback so the save always wins regardless of JS state.
    form["resident_name_1"] = form.get("primary_lessee_name", "")
    new_secondary = form.get("secondary_lessee_name", "").strip()
    if new_secondary:
        form["resident_name_2"] = new_secondary

    note = f"Admin edited submission (status: {record['status']} → {new_status})"
    storage.update_status(
        sid,
        status=new_status,
        event="admin_edited",
        note=note,
        form_data=form,
    )
    log.info("Admin edited submission %s — status %s → %s",
             sid, record["status"], new_status)

    return RedirectResponse(url=f"/admin/submission/{sid}", status_code=303)


@app.post("/admin/submission/{sid}/delete",
          dependencies=[Depends(require_admin)])
async def admin_delete(sid: str):
    """Permanently delete a submission. No emails sent, no SignWell
    interaction. If SignWell already received this document, cancel it
    there manually before deleting here."""
    deleted = storage.delete(sid)
    if not deleted:
        raise HTTPException(status_code=404)
    log.info("Admin permanently deleted submission %s", sid)
    return RedirectResponse(url="/admin/", status_code=303)


# ---------------------------------------------------------------------------
# Template preview (unchanged, admin-only)
# ---------------------------------------------------------------------------


@app.get("/preview")
async def preview(
    token: str = Query(default=""),
    primary_lessee_name: str = Query(default="Jane Prospect"),
    secondary_lessee_name: str = Query(default=""),
    num_lessees: int = Query(default=1),
    start_date: str = Query(default="2026-06-01"),
    end_date: str = Query(default="2027-05-31"),
    has_pets: str = Query(default="no"),
):
    if PREVIEW_TOKEN and token != PREVIEW_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid preview token")

    form = {
        "primary_lessee_name": primary_lessee_name,
        "primary_lessee_email": "preview@example.com",
        "primary_lessee_phone": "555-0100",
        "secondary_lessee_name": secondary_lessee_name,
        "secondary_lessee_email": "preview2@example.com" if secondary_lessee_name else "",
        "secondary_lessee_phone": "555-0101" if secondary_lessee_name else "",
        "num_lessees": str(num_lessees),
        "emergency_contact_name": "Emergency Contact",
        "emergency_contact_phone": "555-0199",
        "start_date": start_date,
        "end_date": end_date,
        "has_pets": has_pets,
        "num_pets": "1" if has_pets == "yes" else "",
        "pet_description": "One cat, indoor only" if has_pets == "yes" else "",
        "resident_name_1": primary_lessee_name,
        "resident_dob_1": "1990-01-01",
        "renters_insurance": "Yes",
    }
    if secondary_lessee_name:
        form["resident_name_2"] = secondary_lessee_name
        form["resident_dob_2"] = "1991-02-02"

    ctx = _build_context(form)
    pdf = _render_lease_pdf(ctx)
    return Response(content=pdf, media_type="application/pdf")
