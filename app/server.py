"""
Mock target application: "Meridian Credit Union — Teller Console"

Deliberately built to resemble a legacy enterprise back-office app:
- server-rendered HTML, table-based layout
- no test IDs / no data-testid attributes anywhere
- non-semantic markup in places (onclick on <span>, nested tables)
- session-based auth with an artificial idle timeout
- injectable failure modes via query params / session flags, so the
  replay engine has real runtime errors to detect (not just happy path)

This app is the "one concrete surface" the assignment asks us to implement
against. It is not meant to be pretty; it is meant to be representative of
the kind of surface described in the brief (Section 1: "The real environment").

Flow implemented (search -> detail -> action -> confirmation):
  1. Login                              (session start)
  2. Member search  (by member ID)
  3. Member detail  (balances, "Open Sub-Account" action)
  4. Open sub-account form (type + opening deposit)
  5. Confirmation screen (with a generated reference number)

Injectable runtime conditions (for replay error-handling demo):
  - member not found            -> GET /member/<id> with unknown id
  - validation error            -> negative / zero opening deposit
  - permission denial           -> member flagged as "restricted"
  - session timeout             -> session idle > SESSION_TIMEOUT_SECONDS,
                                    or `?inject=timeout`
  - unexpected confirmation dialog -> large deposit amount triggers a
                                    supervisor-override interstitial
  - transient slowness/error    -> `?inject=slow` / `?inject=error500`
"""
import random
import string
import time
from functools import wraps

from flask import Flask, request, redirect, url_for, session, render_template_string, jsonify

app = Flask(__name__)
app.secret_key = "dev-only-not-a-real-secret"  # local mock app only

SESSION_TIMEOUT_SECONDS = 180

# --- In-memory "core banking" data -----------------------------------------
MEMBERS = {
    "10001": {"id": "10001", "name": "Alicia Roman", "savings_balance": 4210.55,
              "checking_balance": 812.10, "restricted": False},
    "10002": {"id": "10002", "name": "David Chen", "savings_balance": 155.00,
              "checking_balance": 0.0, "restricted": False},
    "10003": {"id": "10003", "name": "Priya Natarajan", "savings_balance": 98450.02,
              "checking_balance": 4020.33, "restricted": True},  # permission-denial case
    "12345": {"id": "12345", "name": "Marcus Webb", "savings_balance": 2630.77,
              "checking_balance": 512.40, "restricted": False},
}

SUB_ACCOUNTS = []  # created during "open sub-account" flow


def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user"):
            return redirect(url_for("login", next=request.path))
        last_seen = session.get("last_seen", 0)
        if time.time() - last_seen > SESSION_TIMEOUT_SECONDS or request.args.get("inject") == "timeout":
            session.clear()
            return render_template_string(TIMEOUT_HTML), 440  # non-standard "session expired"
        session["last_seen"] = time.time()
        return f(*args, **kwargs)
    return wrapper


# --- Templates (deliberately legacy: tables, no semantic tags, no test ids) ---

BASE_STYLE = """
<style>
  body { font-family: Tahoma, Geneva, sans-serif; font-size: 13px; background:#e8e8e8; margin:0; }
  table { border-collapse: collapse; }
  .chrome { background:#003865; color:white; padding:6px 12px; font-weight:bold; }
  .content { padding: 16px; background: white; margin: 10px; border: 1px solid #999; }
  .fieldtbl td { padding: 4px 8px; }
  .searchbox { border:1px solid #999; padding:2px; }
  .btn { background:#003865; color:white; border:1px solid #002244; padding:4px 14px; cursor:pointer; }
  .err { color:#a00; font-weight:bold; }
  .warn-box { background:#fff3cd; border:1px solid #ffe58f; padding:10px; margin:10px 0; }
  .data-tbl td, .data-tbl th { border:1px solid #ccc; padding:5px 10px; }
</style>
"""

LOGIN_HTML = BASE_STYLE + """
<div class="chrome">Meridian Credit Union &mdash; Teller Console v3.2</div>
<div class="content">
<h3>Sign In</h3>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
<form method="post">
<table class="fieldtbl">
<tr><td>Username</td><td><input class="searchbox" name="username" /></td></tr>
<tr><td>Password</td><td><input class="searchbox" type="password" name="password" /></td></tr>
</table>
<br/>
<span class="btn" onclick="this.closest('form').submit()">Sign In</span>
<input type="submit" style="display:none" />
</form>
</div>
"""

TIMEOUT_HTML = BASE_STYLE + """
<div class="chrome">Meridian Credit Union &mdash; Teller Console v3.2</div>
<div class="content">
<p class="err">Your session has expired due to inactivity. Please sign in again.</p>
<a href="/login">Return to sign in</a>
</div>
"""

SEARCH_HTML = BASE_STYLE + """
<div class="chrome">Meridian Credit Union &mdash; Teller Console v3.2 &nbsp; | &nbsp; Teller: {{ user }} &nbsp; <a style="color:#bcd" href="/logout">Log off</a></div>
<div class="content">
<h3>Member Search</h3>
<form method="get" action="/member-search">
<table class="fieldtbl">
<tr><td>Member ID</td><td><input class="searchbox" name="member_id" value="{{ q or '' }}" /></td>
<td><span class="btn" onclick="this.closest('form').submit()">Search</span></td></tr>
</table>
</form>
{% if searched and not found %}
<p class="err">No member found matching ID "{{ q }}".</p>
{% endif %}
</div>
"""

DETAIL_HTML = BASE_STYLE + """
<div class="chrome">Meridian Credit Union &mdash; Teller Console v3.2 &nbsp; | &nbsp; Teller: {{ user }} &nbsp; <a style="color:#bcd" href="/logout">Log off</a></div>
<div class="content">
<p><a href="/member-search">&laquo; Back to search</a></p>
<h3>Member Detail</h3>
<table class="data-tbl">
<tr><th align="left">Field</th><th align="left">Value</th></tr>
<tr><td>Member ID</td><td>{{ m.id }}</td></tr>
<tr><td>Name</td><td>{{ m.name }}</td></tr>
<tr><td>Savings Balance</td><td>${{ '%.2f'|format(m.savings_balance) }}</td></tr>
<tr><td>Checking Balance</td><td>${{ '%.2f'|format(m.checking_balance) }}</td></tr>
</table>
<br/>
{% if m.restricted %}
<p class="err">This member account is flagged RESTRICTED. Sub-account actions require supervisor authorization and are blocked for standard tellers.</p>
{% else %}
<span class="btn" onclick="window.location='/open-sub-account?member_id={{ m.id }}'">Open Sub-Account</span>
{% endif %}
</div>
"""

FORM_HTML = BASE_STYLE + """
<div class="chrome">Meridian Credit Union &mdash; Teller Console v3.2 &nbsp; | &nbsp; Teller: {{ user }} &nbsp; <a style="color:#bcd" href="/logout">Log off</a></div>
<div class="content">
<p><a href="/member/{{ m.id }}">&laquo; Back to member</a></p>
<h3>Open Sub-Account for {{ m.name }} ({{ m.id }})</h3>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
<form method="post">
<table class="fieldtbl">
<tr><td>Account Type</td><td>
  <select name="account_type">
    <option value="SAVINGS_SUB">Savings Sub-Account</option>
    <option value="CHRISTMAS_CLUB">Christmas Club</option>
  </select>
</td></tr>
<tr><td>Opening Deposit ($)</td><td><input class="searchbox" name="opening_deposit" value="{{ deposit or '' }}" /></td></tr>
</table>
<br/>
<span class="btn" onclick="this.closest('form').submit()">Submit</span>
<input type="submit" style="display:none" />
</form>
</div>
"""

OVERRIDE_HTML = BASE_STYLE + """
<div class="chrome">Meridian Credit Union &mdash; Teller Console v3.2</div>
<div class="content">
<div class="warn-box">
<b>Supervisor Override Required</b><br/>
Opening deposits over $5,000 require supervisor confirmation before the sub-account is created.
</div>
<form method="post" action="/open-sub-account/confirm-override">
<input type="hidden" name="member_id" value="{{ member_id }}" />
<input type="hidden" name="account_type" value="{{ account_type }}" />
<input type="hidden" name="opening_deposit" value="{{ opening_deposit }}" />
<span class="btn" onclick="this.closest('form').submit()">Confirm Override &amp; Continue</span>
<input type="submit" style="display:none" />
<a href="/member/{{ member_id }}" style="margin-left:10px;">Cancel</a>
</form>
</div>
"""

CONFIRM_HTML = BASE_STYLE + """
<div class="chrome">Meridian Credit Union &mdash; Teller Console v3.2</div>
<div class="content">
<h3>Sub-Account Created</h3>
<table class="data-tbl">
<tr><td>Reference #</td><td>{{ ref }}</td></tr>
<tr><td>Member</td><td>{{ m.name }} ({{ m.id }})</td></tr>
<tr><td>Account Type</td><td>{{ account_type }}</td></tr>
<tr><td>Opening Deposit</td><td>${{ '%.2f'|format(deposit) }}</td></tr>
</table>
<p>Transaction complete.</p>
<a href="/member-search">New search</a>
</div>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == "teller1" and password == "demo-pass":
            session["user"] = username
            session["last_seen"] = time.time()
            return redirect(url_for("member_search_page"))
        return render_template_string(LOGIN_HTML, error="Invalid username or password."), 401
    return render_template_string(LOGIN_HTML, error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/member-search")
@require_login
def member_search_page():
    q = request.args.get("member_id")
    if request.args.get("inject") == "error500":
        return "Internal Server Error (injected for testing)", 500
    if request.args.get("inject") == "slow":
        time.sleep(4)
    if q:
        found = q in MEMBERS
        if found:
            return redirect(url_for("member_detail", member_id=q))
        return render_template_string(SEARCH_HTML, user=session["user"], q=q, searched=True, found=False)
    return render_template_string(SEARCH_HTML, user=session["user"], q=None, searched=False, found=None)


@app.route("/member/<member_id>")
@require_login
def member_detail(member_id):
    m = MEMBERS.get(member_id)
    if not m:
        return render_template_string(SEARCH_HTML, user=session["user"], q=member_id, searched=True, found=False)
    return render_template_string(DETAIL_HTML, user=session["user"], m=m)


@app.route("/open-sub-account")
@require_login
def open_sub_account_form():
    member_id = request.args.get("member_id")
    m = MEMBERS.get(member_id)
    if not m:
        return "Unknown member", 404
    if m["restricted"]:
        return "Forbidden: member is restricted", 403
    return render_template_string(FORM_HTML, user=session["user"], m=m, error=None, deposit=None)


@app.route("/open-sub-account", methods=["POST"])
@require_login
def open_sub_account_submit():
    member_id = request.args.get("member_id") or request.form.get("member_id")
    m = MEMBERS.get(member_id)
    if not m:
        return "Unknown member", 404
    account_type = request.form.get("account_type")
    raw_deposit = request.form.get("opening_deposit", "")
    try:
        deposit = float(raw_deposit)
    except ValueError:
        return render_template_string(FORM_HTML, user=session["user"], m=m,
                                       error="Opening deposit must be a valid number.",
                                       deposit=raw_deposit), 400
    if deposit <= 0:
        return render_template_string(FORM_HTML, user=session["user"], m=m,
                                       error="Opening deposit must be greater than $0.00.",
                                       deposit=raw_deposit), 400

    if deposit > 5000:
        # unexpected-confirmation-dialog case: an interstitial the flow must handle
        return render_template_string(OVERRIDE_HTML, member_id=member_id,
                                       account_type=account_type, opening_deposit=deposit)

    return _create_sub_account(m, account_type, deposit)


@app.route("/open-sub-account/confirm-override", methods=["POST"])
@require_login
def confirm_override():
    member_id = request.form.get("member_id")
    m = MEMBERS.get(member_id)
    account_type = request.form.get("account_type")
    deposit = float(request.form.get("opening_deposit"))
    return _create_sub_account(m, account_type, deposit)


def _create_sub_account(m, account_type, deposit):
    ref = "SA-" + "".join(random.choices(string.digits, k=8))
    SUB_ACCOUNTS.append({"ref": ref, "member_id": m["id"], "account_type": account_type, "deposit": deposit})
    return render_template_string(CONFIRM_HTML, m=m, ref=ref, account_type=account_type, deposit=deposit)


@app.route("/")
def index():
    return redirect(url_for("login"))


# Test-only introspection endpoint (never part of the automated surface itself)
@app.route("/_debug/sub_accounts")
def debug_sub_accounts():
    return jsonify(SUB_ACCOUNTS)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
