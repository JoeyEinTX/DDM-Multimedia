// static/js/page_rev.js - a page left open across an update reloads itself
//
// pi5 sends the revision of its pages with every response (X-Page-Rev,
// pi5/page_rev.py) and a page carries the one it was served with
// (<meta name="ddm-page-rev">). The page hands check() the header of the poll
// it already runs; when the two differ it reloads, if idle() says nothing
// would be lost (each page's own rule), else it calls waiting() and the next
// poll tries again. Nothing else ever reloads a page. The one implementation,
// for the LQ admin page and the Control Center (the iPad's Home Screen apps
// keep a page for days, running the script it was loaded with).
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="ddm-page-rev"]');
  var mine = meta ? meta.getAttribute("content") || "" : "";
  window.DDMPageRev = {
    header: "X-Page-Rev",
    mine: function () { return mine; },
    check: function (rev, idle, waiting) {
      if (!rev || !mine || rev === mine) { return false; }
      if (!idle || idle()) { location.reload(); return true; }
      if (waiting) { waiting(); }
      return false;
    }
  };
})();
