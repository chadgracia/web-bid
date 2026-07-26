import json
import html
import os
import time
import hmac
import hashlib
import base64
import urllib.parse

import boto3

# Shared with trades — must match trades' IDENTITY_SECRET env var so we can
# verify the handoff token trades mints. Set in the Lambda console.
IDENTITY_SECRET = os.environ.get("IDENTITY_SECRET", "")

# SES: same sender pattern as the other functions.
SES_SENDER = "agent@agent.graciagroup.com"
SES_REPLY_TO = "cgracia@rainmakersecurities.com"
BID_NOTIFY_TO = "cgracia@rainmakersecurities.com"


def _q(event, key):
    """Read a query-string param from a Lambda Function URL (payload v2)."""
    return (event.get("queryStringParameters") or {}).get(key)


def _lookup_pricing(company_id):
    """Read directory_companies.json (small) and return the pricing entry for
    this company_id, or {} on any miss. Never raises."""
    if not company_id:
        return {}
    try:
        s3 = boto3.client("s3")
        obj = s3.get_object(Bucket="full-pipeline-cache", Key="directory_companies.json")
        data = json.loads(obj["Body"].read().decode("utf-8"))
        return (data.get("pricing") or {}).get(str(company_id)) or {}
    except Exception:
        return {}


def _lookup_holders(company_name):
    """Read holder_counts.json and return the holder count for this company name
    (case-insensitive), or None on any miss. Never raises."""
    if not company_name:
        return None
    try:
        s3 = boto3.client("s3")
        obj = s3.get_object(Bucket="full-pipeline-cache", Key="holder_counts.json")
        data = json.loads(obj["Body"].read().decode("utf-8"))
        counts = data.get("counts") or {}
        target = company_name.strip().lower()
        for name, n in counts.items():
            if name.strip().lower() == target:
                return n
        return None
    except Exception:
        return None


def _resolve_company_by_name(name):
    """Anonymous entry: given a company NAME, find its directory entry.
    Returns (company_id, display_name, pricing_entry) or ("", name, {}).
    Case-insensitive match against the pricing map's stored names. Never raises."""
    if not name:
        return "", "", {}
    try:
        s3 = boto3.client("s3")
        obj = s3.get_object(Bucket="full-pipeline-cache", Key="directory_companies.json")
        data = json.loads(obj["Body"].read().decode("utf-8"))
        pricing = data.get("pricing") or {}
        target = name.strip().lower()
        for cid, entry in pricing.items():
            if (entry.get("name") or "").strip().lower() == target:
                return str(cid), entry.get("name") or name, entry
        return "", name, {}
    except Exception:
        return "", name, {}


def _valid_email(s):
    """Very light email sanity check."""
    s = (s or "").strip()
    return "@" in s and "." in s.split("@")[-1] and len(s) >= 6


def _method(event):
    """HTTP method from a Function URL (payload v2) event."""
    return (((event.get("requestContext") or {}).get("http") or {}).get("method")
            or event.get("httpMethod") or "GET").upper()


def _verify_handoff_token(token):
    """Reverse of trades' _make_handoff_token:
      base64url(email|exp|sig), sig = HMAC-SHA256(IDENTITY_SECRET, "email|exp").
    Returns the verified email if the signature is valid AND not expired, else None.
    Never raises."""
    if not IDENTITY_SECRET or not token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
        email, exp, sig = decoded.rsplit("|", 2)
        expected = hmac.new(IDENTITY_SECRET.encode(), f"{email}|{exp}".encode(),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        if int(exp) < int(time.time()):
            return None
        return email
    except Exception:
        return None


def _json_response(status, obj):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(obj),
    }


def _send_bid_email(email, company_id, company_name, gross, min_size, max_size,
                    structure, source="logged-in", submitter_name=""):
    """Email the bid to Chad. No Pipeline write yet — this is the safe first step."""
    ses = boto3.client("ses", region_name="us-east-1")
    struct_txt = ", ".join(structure) if structure else "(none)"
    subject = f"New web bid: {company_name} @ ${gross}/sh"
    if source == "anonymous":
        who = f"Anonymous lead: {submitter_name} <{email}>"
    else:
        who = f"Logged-in user: {email}"
    body = (
        f"A firm buy bid was submitted through the web-bid page.\n\n"
        f"Company:      {company_name} (id {company_id})\n"
        f"Bidder:       {who}\n"
        f"Gross price:  ${gross}/share\n"
        f"Min size:     ${min_size}\n"
        f"Max size:     ${max_size}\n"
        f"Structure:    {struct_txt}\n\n"
        f"(No Pipeline deal was created — this is the email-only step.)\n"
    )
    ses.send_email(
        Source=SES_SENDER,
        Destination={"ToAddresses": [BID_NOTIFY_TO]},
        ReplyToAddresses=[SES_REPLY_TO],
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        },
    )


def _handle_post(event):
    """Process a submitted bid. Requires a valid handoff token (sso)."""
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:
            return _json_response(400, {"ok": False, "error": "Bad request."})
    try:
        data = json.loads(raw)
    except Exception:
        return _json_response(400, {"ok": False, "error": "Bad request."})

    # Identity: either a valid handoff token (logged-in trades user) OR a
    # submitted first name + email (anonymous lead arriving from an email link).
    email = _verify_handoff_token(data.get("sso") or "")
    submitter_name = ""
    source = "logged-in"
    if not email:
        submitter_name = str(data.get("submitter_name") or "").strip()
        submitter_email = str(data.get("submitter_email") or "").strip()
        if not submitter_name:
            return _json_response(400, {"ok": False, "error": "Please enter your first name."})
        if not _valid_email(submitter_email):
            return _json_response(400, {"ok": False, "error": "Please enter a valid email."})
        email = submitter_email
        source = "anonymous"

    try:
        gross = float(data.get("gross"))
        min_size = float(data.get("min_size"))
        max_size = float(data.get("max_size"))
    except (TypeError, ValueError):
        return _json_response(400, {"ok": False, "error": "Invalid numbers."})

    structure = data.get("structure") or []
    allowed = {"Direct", "Fund", "Forward"}
    structure = [s for s in structure if s in allowed]

    company_id = str(data.get("company_id") or "")
    company_name = str(data.get("company_name") or "")

    if not (gross > 0):
        return _json_response(400, {"ok": False, "error": "Gross price must be greater than 0."})
    if not (min_size > 0):
        return _json_response(400, {"ok": False, "error": "Min size must be greater than 0."})
    if not (max_size > 0):
        return _json_response(400, {"ok": False, "error": "Max size must be greater than 0."})
    if min_size > max_size:
        return _json_response(400, {"ok": False, "error": "Min size must be <= max size."})
    if not structure:
        return _json_response(400, {"ok": False, "error": "Select at least one structure."})
    if not company_id:
        return _json_response(400, {"ok": False, "error": "Missing company."})

    try:
        _send_bid_email(email, company_id, company_name, gross, min_size, max_size,
                        structure, source=source, submitter_name=submitter_name)
    except Exception as e:
        return _json_response(500, {"ok": False, "error": "Could not send. Try again."})

    return _json_response(200, {"ok": True})


def _render_page(event):
    company_id = _q(event, "bid") or ""
    company_name = _q(event, "name") or ""
    sso = _q(event, "sso") or ""
    company_param = _q(event, "company") or ""

    # Identity: a VALID handoff token means logged-in; otherwise anonymous.
    _verified_email = _verify_handoff_token(sso)
    is_anonymous = _verified_email is None

    # Anonymous entry via ?company=Name (no id): resolve name -> directory entry
    # so we still get pricing + the correct display name.
    if company_param and not company_id:
        rid, rname, _ = _resolve_company_by_name(company_param)
        company_id = rid or company_id
        company_name = rname or company_param

    safe_name = html.escape(company_name) if company_name else "this company"
    safe_id = html.escape(company_id)
    safe_sso = html.escape(sso)

    # Market-price nudge (from directory_companies.json). Rounded, vague label,
    # shown only when a market reference exists.
    _pricing = _lookup_pricing(company_id)
    _market = _pricing.get("market")
    if _market:
        _market_html = (
            f'<div style="flex:0 0 auto;font-size:12px;color:var(--text-secondary);'
            f'line-height:1.35;padding-top:2px;">'
            f'<span style="color:var(--ink);font-weight:600;">Market ~${round(float(_market)):,}/share</span><br>'
            f'Below-market bids may be ignored.</div>'
        )
    else:
        _market_html = ""

    # Last-round data for the live implied-valuation readout (JS computes on input).
    # lr_val is in BILLIONS (0.9 = $900M).
    _lr_pps = _pricing.get("lr_pps")
    _lr_val = _pricing.get("lr_val")
    _val_pps_js = float(_lr_pps) if isinstance(_lr_pps, (int, float)) else 0
    _val_lrv_js = float(_lr_val) if isinstance(_lr_val, (int, float)) else 0

    # Anonymous contact fields (side by side) — shown only when no valid logged-in token.
    if is_anonymous:
        _contact_html = (
            '<div class="row" style="margin-top:18px;">'
            '<div><div class="fieldlabel">Your first name</div>'
            '<input id="submitter_name" type="text" placeholder="First name"></div>'
            '<div><div class="fieldlabel">Your email</div>'
            '<input id="submitter_email" type="email" placeholder="you@example.com"></div>'
            '</div>'
        )
    else:
        _contact_html = ""

    # Holder count (from holder_counts.json, keyed by company name).
    _holders = _lookup_holders(company_name)
    if _holders and _holders > 0:
        _holder_line = (
            f"We don't currently have an active seller for {safe_name}, but "
            f"<strong>{_holders:,} holders</strong> in our system have shares. "
            f"Send your firm bid and we'll let you know if any of them accept it."
        )
    else:
        _holder_line = (
            f"We don't currently have an active seller for {safe_name}, but we're in touch "
            f"with holders. Send your firm bid and we'll let you know if any of them accept it."
        )

    is_anon_js = "true" if is_anonymous else "false"

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Place a Bid — {safe_name}</title>
<link rel="stylesheet" href="https://s3.us-east-1.amazonaws.com/main.css/master.css">
<style>
  /* Only local layout not covered by master.css — colors/fonts/inputs/buttons
     all inherit from master.css design tokens. */
  * {{ box-sizing: border-box; }}
  body {{ padding: 24px; }}
  .card {{ max-width: 560px; margin: 24px auto; padding: 32px; }}
  .card h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: var(--text-secondary); margin: 0 0 24px; font-size: 14px; }}
  .hint {{ font-weight: 400; color: var(--text-muted); font-size: 12px; }}
  .fieldlabel {{ display: block; font-weight: 600; color: var(--text-secondary); margin: 18px 0 6px; font-size: 13px; }}
  .row {{ display: flex; gap: 12px; }}
  .row > div {{ flex: 1; }}
  .struct {{ display: flex; gap: 16px; margin-top: 6px; }}
  #submitBtn {{ width: 100%; margin-top: 28px; padding: 14px; font-size: 15px; font-weight: 600; }}
  .overlay {{
    display: none; position: fixed; inset: 0; background: rgba(20,24,29,0.5);
    align-items: center; justify-content: center;
  }}
  .overlay.show {{ display: flex; }}
  .modal {{ background: var(--surface); border-radius: var(--radius-lg); max-width: 400px; padding: 32px; text-align: center; box-shadow: var(--shadow); }}
  .modal h2 {{ margin: 0 0 8px; font-size: 18px; color: var(--ink); }}
  .modal p {{ color: var(--text-secondary); margin: 0; }}
</style>
</head>
<body>
  <div class="card">
    <h1>Place a bid on {safe_name}</h1>
    <p class="sub">{_holder_line}</p>

    <div class="fieldlabel">Gross price per share <span class="hint">(USD)</span></div>
    <div style="display:flex;gap:12px;align-items:flex-start;">
      <input id="gross" type="number" step="0.01" min="0" placeholder="e.g. 45.00" style="flex:0 0 calc(50% - 6px);">
      {_market_html}
    </div>
    <div id="impliedVal" style="font-size:12px;color:var(--text-secondary);margin-top:6px;display:none;"></div>

    <div class="row">
      <div>
        <div class="fieldlabel">Min size <span class="hint">($)</span></div>
        <input id="min_size" type="number" step="1000" min="0" placeholder="e.g. 250000">
      </div>
      <div>
        <div class="fieldlabel">Max size <span class="hint">($)</span></div>
        <input id="max_size" type="number" step="1000" min="0" placeholder="e.g. 1000000">
      </div>
    </div>

    <div class="fieldlabel">Structure</div>
    <div class="struct">
      <label class="checkbox-label"><input type="checkbox" class="struct-box" value="Direct"> Direct</label>
      <label class="checkbox-label"><input type="checkbox" class="struct-box" value="Fund"> Fund / SPV</label>
      <label class="checkbox-label"><input type="checkbox" class="struct-box" value="Forward"> Forward</label>
    </div>

    {_contact_html}

    <p id="err" style="color:var(--neg);font-size:13px;margin:16px 0 0;display:none;"></p>
    <button id="submitBtn" class="bid-button" onclick="submitBid()">Submit firm bid</button>
  </div>

  <div class="overlay" id="overlay">
    <div class="modal">
      <h2>Bid received</h2>
      <p>Thanks — we'll be in touch.</p>
    </div>
  </div>

  <script>
    var COMPANY_ID = "{safe_id}";
    var COMPANY_NAME = "{safe_name}";
    var SSO = "{safe_sso}";
    var LR_PPS = {_val_pps_js};
    var LR_VAL = {_val_lrv_js};
    var IS_ANONYMOUS = {is_anon_js};

    function showErr(msg) {{
      var e = document.getElementById('err');
      e.textContent = msg;
      e.style.display = 'block';
    }}

    function fmtVal(billions) {{
      if (billions >= 1) {{
        return '$' + billions.toFixed(1) + 'B';
      }}
      return '$' + Math.round(billions * 1000).toLocaleString() + 'M';
    }}

    function updateImplied() {{
      var el = document.getElementById('impliedVal');
      var price = parseFloat(document.getElementById('gross').value);
      if (LR_PPS > 0 && LR_VAL > 0 && price > 0) {{
        var implied = LR_VAL * (price / LR_PPS);
        el.innerHTML = '≈ <strong>' + fmtVal(implied) + '</strong> implied valuation · based on last public round, estimate only';
        el.style.display = 'block';
      }} else {{
        el.style.display = 'none';
      }}
    }}

    document.getElementById('gross').addEventListener('input', updateImplied);

    function submitBid() {{
      var gross = parseFloat(document.getElementById('gross').value);
      var minSize = parseFloat(document.getElementById('min_size').value);
      var maxSize = parseFloat(document.getElementById('max_size').value);
      var structure = Array.prototype.slice.call(document.querySelectorAll('.struct-box:checked')).map(function(b) {{ return b.value; }});

      if (!(gross > 0)) {{ showErr('Enter a gross price per share greater than 0.'); return; }}
      if (!(minSize > 0)) {{ showErr('Enter a minimum size greater than 0.'); return; }}
      if (!(maxSize > 0)) {{ showErr('Enter a maximum size greater than 0.'); return; }}
      if (minSize > maxSize) {{ showErr('Minimum size must be less than or equal to maximum size.'); return; }}
      if (structure.length === 0) {{ showErr('Select at least one structure.'); return; }}

      var subName = "", subEmail = "";
      if (IS_ANONYMOUS) {{
        var nEl = document.getElementById('submitter_name');
        var eEl = document.getElementById('submitter_email');
        subName = nEl ? nEl.value.trim() : "";
        subEmail = eEl ? eEl.value.trim() : "";
        if (!subName) {{ showErr('Enter your first name.'); return; }}
        if (subEmail.indexOf('@') === -1) {{ showErr('Enter a valid email.'); return; }}
      }}

      document.getElementById('err').style.display = 'none';
      var btn = document.getElementById('submitBtn');
      btn.disabled = true;
      btn.textContent = 'Submitting…';

      fetch(window.location.pathname, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{
          company_id: COMPANY_ID,
          company_name: COMPANY_NAME,
          gross: gross,
          min_size: minSize,
          max_size: maxSize,
          structure: structure,
          sso: SSO,
          submitter_name: subName,
          submitter_email: subEmail
        }})
      }})
      .then(function(r) {{ return r.json().then(function(j) {{ return {{ status: r.status, body: j }}; }}); }})
      .then(function(res) {{
        if (res.status === 200 && res.body.ok) {{
          document.getElementById('overlay').classList.add('show');
          setTimeout(function() {{ window.location.href = 'https://trades.graciagroup.com'; }}, 2500);
        }} else {{
          btn.disabled = false;
          btn.textContent = 'Submit firm bid';
          showErr(res.body.error || 'Something went wrong. Try again.');
        }}
      }})
      .catch(function() {{
        btn.disabled = false;
        btn.textContent = 'Submit firm bid';
        showErr('Network error. Try again.');
      }});
    }}
  </script>
</body>
</html>"""

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html"},
        "body": page,
    }


def lambda_handler(event, context):
    if _method(event) == "POST":
        return _handle_post(event)
    return _render_page(event)
