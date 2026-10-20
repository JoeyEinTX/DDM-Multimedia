/* =====================================================================
   A TV page's self-reload and its "I'm up" to the TV box.

   Loaded by every page a TV shows (templates/slideshow.html: /display and
   /living-room; templates/board.html: /board, the La Quiniela page).

   Self-reload (47c7d88, d4aa4ba): GET /api/page_rev every PAGE_REV_CHECK_S
   (15 s; ?page_rev_s= for a test). When the splash serves another revision
   than this page's (<meta name="ddm-page-rev">), the page reloads at the
   next quiet moment the page offers (window.ddmKiosk.quietMoment(): the
   slideshow calls it between slides), or three answering checks later at
   the latest (well within a minute; the board page, with no slides, always
   reloads this way). Nothing else ever reloads it, and it never reloads
   into a splash that is not answering at that moment: a reload into a dead
   server is Chromium's error page, which nothing brings back. Until it
   answers, the page it has stays up.

   "I'm up": on the TV box the page runs in a frame of the box's own holding
   page (deploy/tv_box/holding.html), which shows "Waiting for DDM" instead
   when it stops hearing this: the page says it is up, every 2 s.
   ===================================================================== */
(function () {
    'use strict';

    const PAGE_REV = (document.querySelector('meta[name="ddm-page-rev"]') || {}).content || '';
    const PAGE_REV_CHECK_S = Math.max(0.2, Number(new URLSearchParams(location.search).get('page_rev_s')) || 15);
    let changed = 0;                             // answering checks since another revision was seen; 0: none
    let reloading = false;

    async function servedPageRev() {             // the revision the splash serves now; null: it does not answer
        try {
            const resp = await fetch('/api/page_rev', { cache: 'no-store' });
            return resp.ok ? resp.headers.get('X-Page-Rev') : null;
        } catch (err) {
            return null;
        }
    }

    async function check() {
        const rev = await servedPageRev();
        if (!rev || !PAGE_REV) return;           // not answering: never reload into it
        if (rev === PAGE_REV) { changed = 0; return; }
        changed += 1;
        if (changed > 2) location.reload();      // it answered just now
    }

    async function reloadIfAnswering() {
        if (reloading) return;
        reloading = true;
        const rev = await servedPageRev();
        if (rev && PAGE_REV && rev !== PAGE_REV) location.reload();
        else reloading = false;
    }

    setInterval(check, PAGE_REV_CHECK_S * 1000);

    if (window.parent !== window) {
        const beat = () => window.parent.postMessage({ ddm: 'board', rev: PAGE_REV }, '*');
        beat();
        setInterval(beat, 2000);
    }

    window.ddmKiosk = {
        rev: PAGE_REV,
        quietMoment() {                          // between slides: reload now if a new revision is waiting
            if (changed) reloadIfAnswering();
        },
    };
})();
