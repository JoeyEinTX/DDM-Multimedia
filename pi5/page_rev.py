# pi5/page_rev.py - the revision of the pages pi5 serves, so they come up fresh after a pull
#
# Joey, 2026-10-14: the iPad's two Home Screen apps (DDM, the Control Center,
# and LQ Admin) and the TV kiosk showed stale pages after a pull more than once.
# PAGE_REV is a short hash of every template and every static JS and CSS file
# pi5 serves, read once when pi5 starts. Flask reads a template once too, so the
# revision changes exactly when the pages served change: a pull and a restart.
# It is
#   - the ?v= of every static URL (static_url), so a changed file is a new URL
#     and the browser's normal cache can keep everything else;
#   - the X-Page-Rev header of every response pi5 sends, which an open page
#     compares with the revision it was served with (<meta name="ddm-page-rev">)
#     on the poll it already runs, reloading itself when they differ and it is
#     idle (static/js/page_rev.js: the LQ admin page and the Control Center);
# and every HTML response is Cache-Control: no-store (init_app's after_request,
# the one place: every page, La Subasta's included). API responses keep their
# own Cache-Control.

import hashlib
import os

from flask import url_for

HERE = os.path.dirname(os.path.abspath(__file__))
HEADER = "X-Page-Rev"
DIRS = ("templates", os.path.join("la_quiniela", "templates"), os.path.join("la_subasta", "templates"), "static")
KINDS = (".html", ".js", ".css")


def compute(root: str = HERE) -> str:
    """Twelve hex digits of a SHA-1 over every .html, .js and .css under DIRS,
    path and content, in a fixed order."""
    digest = hashlib.sha1()
    for d in DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, d)):
            dirnames.sort()
            for name in sorted(filenames):
                if not name.endswith(KINDS):
                    continue
                path = os.path.join(dirpath, name)
                digest.update(os.path.relpath(path, root).replace(os.sep, "/").encode("utf-8"))
                try:
                    with open(path, "rb") as fh:
                        digest.update(fh.read())
                except OSError:
                    pass
    return digest.hexdigest()[:12]


PAGE_REV = compute()


def static_url(filename: str) -> str:
    """url_for('static') with ?v=PAGE_REV."""
    return url_for("static", filename=filename, v=PAGE_REV)


def init_app(app) -> None:
    """static_url and page_rev for every template; X-Page-Rev on every
    response; Cache-Control: no-store on every HTML response."""
    app.jinja_env.globals["static_url"] = static_url

    @app.context_processor
    def _page_rev():
        return {"page_rev": PAGE_REV}

    @app.after_request
    def _fresh_pages(response):
        response.headers[HEADER] = PAGE_REV
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store"
        return response
