import json
import html


def _q(event, key):
    """Read a query-string param from a Lambda Function URL (payload v2)."""
    return (event.get("queryStringParameters") or {}).get(key)


def lambda_handler(event, context):
    company_id = _q(event, "bid") or ""
    company_name = _q(event, "name") or ""

    safe_name = html.escape(company_name) if company_name else "this company"
    safe_id = html.escape(company_id)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Place a Bid — {safe_name}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #f5f5f5; color: #1a1a1a; margin: 0; padding: 24px;
    font-size: 14px; line-height: 1.5;
  }}
  .card {{
    background: #fff; border-radius: 12px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    max-width: 560px; margin: 24px auto; padding: 32px;
  }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: #6b7280; margin: 0 0 24px; font-size: 14px; }}
  label {{ display: block; font-weight: 600; margin: 18px 0 6px; }}
  .hint {{ font-weight: 400; color: #9ca3af; font-size: 12px; }}
  input, select {{
    width: 100%; padding: 11px 12px; border: 1px solid #d1d5db;
    border-radius: 8px; font-size: 15px; font-family: inherit; background: #fff;
  }}
  input:focus, select:focus {{ outline: none; border-color: #1a1a1a; }}
  .row {{ display: flex; gap: 12px; }}
  .row > div {{ flex: 1; }}
  .struct {{ display: flex; gap: 16px; margin-top: 6px; }}
  .struct label {{ font-weight: 400; margin: 0; display: flex; align-items: center; gap: 6px; cursor: pointer; }}
  .struct input {{ width: auto; }}
  button {{
    width: 100%; margin-top: 28px; background: #1a1a1a; color: #fff; border: none;
    padding: 14px; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer;
  }}
  button:hover {{ background: #333; }}
  .overlay {{
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.5);
    align-items: center; justify-content: center;
  }}
  .overlay.show {{ display: flex; }}
  .modal {{ background: #fff; border-radius: 12px; max-width: 400px; padding: 32px; text-align: center; }}
  .modal h2 {{ margin: 0 0 8px; font-size: 18px; }}
  .modal p {{ color: #6b7280; margin: 0; }}
</style>
</head>
<body>
  <div class="card">
    <h1>Place a bid on {safe_name}</h1>
    <p class="sub">We don't currently have a live seller for {safe_name}. Submit a firm bid and we'll notify you if a match comes up.</p>

    <label>Gross price per share <span class="hint">(USD)</span></label>
    <input id="gross" type="number" step="0.01" min="0" placeholder="e.g. 45.00">

    <div class="row">
      <div>
        <label>Min size <span class="hint">($)</span></label>
        <input id="min_size" type="number" step="1000" min="0" placeholder="e.g. 250000">
      </div>
      <div>
        <label>Max size <span class="hint">($)</span></label>
        <input id="max_size" type="number" step="1000" min="0" placeholder="e.g. 1000000">
      </div>
    </div>

    <label>Structure</label>
    <div class="struct">
      <label><input type="checkbox" class="struct-box" value="Direct"> Direct</label>
      <label><input type="checkbox" class="struct-box" value="Fund"> Fund / SPV</label>
      <label><input type="checkbox" class="struct-box" value="Forward"> Forward</label>
    </div>

    <p id="err" style="color:#b91c1c;font-size:13px;margin:16px 0 0;display:none;"></p>
    <button onclick="submitBid()">Submit firm bid</button>
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

    function showErr(msg) {{
      var e = document.getElementById('err');
      e.textContent = msg;
      e.style.display = 'block';
    }}

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

      document.getElementById('err').style.display = 'none';

      var payload = {{
        company_id: COMPANY_ID,
        company_name: COMPANY_NAME,
        gross: gross,
        min_size: minSize,
        max_size: maxSize,
        structure: structure
      }};
      console.log('bid payload (validated, not yet sent):', payload);
      document.getElementById('overlay').classList.add('show');
    }}
  </script>
</body>
</html>"""

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "text/html"},
        "body": page,
    }
