# host_pin.py - The host's PIN: one for every host page and every host write on pi5
#
# Port 5000 is where every guest's phone is pointed for La Subasta, so a phone can
# reach everything the Control Center and the LQ admin page do. Those take the PIN
# in pi5/.env (DDM_HOST_PIN, handed to the app as config HOST_PIN; Joey, 2026-10-30,
# after the 2026-10-28 review's list of writes any phone can call). What needs it:
#
#   - the host pages: the Control Center (/), the LQ admin page and La Subasta's;
#   - La Subasta's /api/admin routes, reads too (they list guests and money);
#   - every other request that changes something (POST, PUT, DELETE), except the
#     four a guest's phone makes (GUEST_WRITES). A new write route is the host's
#     until it is listed there.
#
# Reads stay open: the board, the pages' polls, the streams, the TV and the splash,
# which only read. A page asked for without the PIN is the PIN form; the right PIN
# sets a cookie that holds a keyed hash of it, so the iPad is not asked again for 30
# days, a restart of pi5 does not log it out and a changed PIN does. With no PIN set
# the host pages and writes are off (503), never open. There is no lockout.

import hashlib
import hmac

from flask import Blueprint, current_app, jsonify, render_template, request

host_pin_bp = Blueprint("host_pin", __name__, template_folder="templates")

HOST_COOKIE = "ddm_host"
HOST_COOKIE_MAX_AGE = 30 * 24 * 3600
LOGIN = "/host/login"
HOST_PAGES = ("/", "/quiniela/admin", "/la-subasta/admin")
HOST_ROUTES = "/la-subasta/api/admin/"
GUEST_WRITES = ("/la-subasta/api/register", "/la-subasta/api/bid", "/la-subasta/api/bid/undo",
                "/la-subasta/api/check-identity")
READS = ("GET", "HEAD", "OPTIONS")


def pin_cookie(pin: str) -> str:
    return hmac.new(pin.encode("utf-8"), b"ddm host", hashlib.sha256).hexdigest()


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _refused(page: bool, status: int, error: str):
    if page:
        return render_template("host_pin.html", pin_unset=status == 503), status
    return jsonify({"success": False, "error": error}), status


def _pin() -> str:
    return current_app.config.get("HOST_PIN") or ""


@host_pin_bp.before_app_request
def _the_host_needs_the_pin():
    rule = request.url_rule.rule if request.url_rule else ""
    page = rule in HOST_PAGES
    if not rule or rule == LOGIN or not (page or rule.startswith(HOST_ROUTES)
                                         or (request.method not in READS and rule not in GUEST_WRITES)):
        return None
    if not _pin():
        return _refused(page, 503, "No host PIN is set: put DDM_HOST_PIN=... in pi5/.env and restart pi5")
    if _same(request.cookies.get(HOST_COOKIE, ""), pin_cookie(_pin())):
        return None
    return _refused(page, 401, "The host's PIN is needed: open the Control Center and enter it")


@host_pin_bp.route(LOGIN, methods=["POST"])
def host_login():
    """Body JSON: {"pin"}. 200 and the cookie for the right PIN; 401 "Wrong PIN" for
    anything else, the same line whatever was sent (it says nothing about how close it
    was); 503 with no PIN set."""
    if not _pin():
        return _refused(False, 503, "No host PIN is set: put DDM_HOST_PIN=... in pi5/.env and restart pi5")
    data = request.get_json(silent=True)
    given = data.get("pin") if isinstance(data, dict) else None
    if not isinstance(given, str) or not _same(given, _pin()):
        return jsonify({"success": False, "error": "Wrong PIN"}), 401
    response = jsonify({"success": True})
    response.set_cookie(HOST_COOKIE, pin_cookie(_pin()), max_age=HOST_COOKIE_MAX_AGE, httponly=True, samesite="Strict", path="/")
    return response
