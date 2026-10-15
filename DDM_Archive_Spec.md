# DDM Archive Spec

**Version:** 1.0
**Date:** 2026-10-14
**Target:** DDM 2027
**Parent:** `DDM_System_Architecture_Spec.md` v1.1, section 16

The DDM Archive is a memory of every Derby de Mayo: results, photos, and the stories, one
page per year, kept on pi5 and shown on the Control Center and the TV.

Past DDMs were barely recorded beyond a few photos. From DDM 2027 on, the system records
each year as it happens, so the archive fills itself in rather than relying on someone
remembering to take notes.

Markers are the parent spec's: **[DECIDED]**, **[BUILT]**, **[TO BUILD]**, **[PROPOSED]**.
Only the archive's existence is decided; everything below is proposed until Joey confirms
it.

---

## 1. Principles

1. **An archive year is a frozen copy.** [PROPOSED] Close-out copies a year's figures into
   the archive's own tables. Nothing in the archive reads the live La Quiniela, La Subasta
   or Derby Dash tables, so later changes to those (new columns, a Reset betting, next
   year's race) can never change or break an old year.
2. **The archive must not cost anything on race night.** [PROPOSED] No step of the race
   waits on it. A failed photo, a full disk or a missing webcam breaks nothing else
   (parent principle 5, loose coupling).
3. **Photos never go in git or in SQLite.** [PROPOSED] They are files on DevPi's disk; the
   database holds their names and captions.
4. **The Pi copy is not the only copy.** [PROPOSED] Full-size originals live off the Pi, and
   the archive is copied off the box on a schedule (section 8).
5. **A guest's photo is shown only after the host approves it.** [PROPOSED]

---

## 2. What a year holds

| Part | Contents | How it gets there |
|---|---|---|
| The basics | Year, date, theme or tagline, one line summing up the year, free notes | Typed by the host |
| The Derby | The real race: winner, place, show, the winner's odds, race name | Copied at close-out from La Quiniela's race info and results; typed for past years (public record) |
| La Quiniela | Pot (counted if entered, else at the post), the three prizes, bets per horse with names, scratches | Close-out |
| The betting curve | Tokens per horse over the night, one point per horse per minute | Close-out, downsampled from `telemetry` |
| La Subasta | Each horse's sale price, the top bid, the pot, the winning horse's sale | Close-out |
| Derby Dash | The night's top scores | Close-out, once Derby Dash posts scores (parent section 9) |
| Photos | Host uploads, approved guest uploads, race-state webcam shots | Sections 4–6 |
| Awards | Best hat, best outfit, and any other vote | Section 7, or typed |
| Highlights | Short entries: a funny moment, a first ("first year with wireless cups") | Typed by the host |
| Art | The invite, the trophy, the drink menu, the theme art | Uploaded as photos, tagged `art` |

**Names.** Whether the archive records guest names (La Quiniela draw winners, La Subasta
buyers, award winners, the sign-in) is open question 1. Until it is decided, every name
field is optional free text, entered by hand, and close-out copies none.

---

## 3. Data

New tables in pi5's SQLite file, `pi5/data/la_subasta.db`. [PROPOSED]

| Table | Holds |
|---|---|
| `archive_years` | One row per year: date, theme, tagline, summary line, notes, closed-out time |
| `archive_snapshot` | One JSON document per year per source (`quiniela`, `subasta`, `dash`, `race`): the frozen close-out copy. A JSON document rather than columns, so a future change to La Quiniela's shape cannot break an old year |
| `archive_photos` | File name, year, source (`host`, `guest`, `webcam`, `art`), caption, status (`pending`, `approved`, `hidden`), the race state it was taken in (webcam), taken and uploaded times |
| `archive_highlights` | Year, text, order |
| `archive_awards` | Year, award name, winner text (optional), photo |

Files:

```
pi5/data/archive/<year>/full/<id>.jpg    resized, longest edge 2048 px
pi5/data/archive/<year>/thumb/<id>.jpg   longest edge 400 px
pi5/data/archive/<year>/clips/<id>.mp4   webcam clips (section 6)
```

`pi5/data/archive/` is git-ignored.

---

## 4. Host photo upload [PROPOSED]

- An **Archive admin page** at `/archive/admin`, beside the LQ admin page and on the same
  terms (no PIN, parent section 10). Linked from the Control Center menu.
- Pick a year, select a batch of photos from the iPad or phone camera roll, upload.
- On arrival, pi5 resizes each photo, makes a thumbnail, rotates it upright from its EXIF
  orientation, strips its location data, and converts HEIC to JPG (`pillow-heif`). The
  original is not kept on the Pi.
- Host uploads are approved on arrival.
- The same page edits the basics, highlights, awards and captions, hides or deletes photos,
  and holds the close-out button (section 9).

---

## 5. Guest photo drop [PROPOSED]

The fix for "nobody catalogs the party": every guest's camera roll feeds the archive.

- A guest page at `/memories` on port 5000, the same server guests already reach for
  La Subasta. One button: add photos. Optional caption. No sign-in.
- A **QR code** to it runs on a slide in the splash rotation and on printed table cards.
- **The QR code carries DevPi's IP address, not `joeydevpi.local`.** Many Android phones do
  not resolve `.local` names. This needs DevPi's DHCP reservation (parent section 11).
- Guest uploads land as `pending` and show nowhere until approved on the Archive admin
  page. Approval can be done the next day.
- Limits: 25 photos per upload, 25 MB per photo, video not accepted. pi5 refuses uploads
  when the disk is below 2 GB free, and says so on the page.
- Uploads are processed in the background, one at a time, so a burst of guest uploads
  cannot slow the betting board.

---

## 6. Race-state webcam [PROPOSED]

Gets the room's reaction every year without anyone remembering to take a photo.

- A USB webcam on DevPi, aimed at the room.
- When the race state reaches 3 AT_THE_POST, 4 RUNNING and 5 WINNER, pi5 takes a burst of
  three stills.
- From 4 RUNNING it also records one clip, ending at 5 WINNER plus 30 s, or after 4 minutes,
  whichever is first.
- Webcam shots are filed as `webcam`, approved on arrival, tagged with their race state.
- **It hangs off `BettingBoard.set_race_state()` as a fire-and-forget hook.** The state
  change never waits on the camera. No camera, a busy camera or a full disk is logged to
  `events` and nothing else happens.
- Off by default; `DDM_ARCHIVE_WEBCAM` turns it on.
- The webcam itself is to be bought (open question 3).

---

## 7. Guest extras [PROPOSED, lower priority]

- **Best hat / best outfit vote** on guest phones, from a page beside `/memories`. The host
  opens and closes the vote on the Archive admin page; the winner and their photo go to
  `archive_awards`.
- **Sign-in at the door** on the iPad: a headcount, and a "first DDM" mark. Depends on open
  question 1.

---

## 8. Backups [PROPOSED, must-do]

The archive is the one part of the system pi5 cannot rebuild.

- A nightly job copies `pi5/data/archive/` and a SQLite backup (`.backup`, not a file copy
  of a live database) off the box, to the Shop PC or the NAS (open question 4).
- An **Export year** button on the Archive admin page downloads one year as a zip: photos,
  clips, and a JSON of everything else.
- Full-size originals from Joey's own camera roll stay in cloud photo storage or on the Shop
  PC; the Pi holds the resized copy.
- This also covers the parent spec's `SD card failure` row: the same job backs up the
  betting tables.

---

## 9. Close-out [PROPOSED]

- **A `Close out DDM <year>` button on the Archive admin page.** The year is taken from
  `lq_race`.
- It writes the year's `archive_snapshot` documents from: race info and names, `results.json`,
  the figures at the post, the counted pot, scratches, the betting curve from `telemetry`,
  La Subasta's sales, and Derby Dash's scores.
- **It is safe to press twice.** A second press overwrites only that year's snapshots and
  never touches photos, highlights, awards or notes. The page shows when it was last run.
- **Run it after the draw, before any Reset betting or RESET**, because those clear the
  figures. `RACE_NIGHT.md` gets this step.
- [PROPOSED] Also offer close-out automatically: if Reset betting is pressed while the state
  is 5 WINNER and the year has not been closed out, the admin page asks first.

---

## 10. Showing the archive [PROPOSED]

- **Archive pages** at `/archive`: a list of years, newest first, and one page per year with
  the results, the betting curve, the gallery, highlights and awards. Touch-friendly, in
  the Control Center's look, and fine on the iPad.
- **"DDM Through the Years" slide** in the splash rotation: approved photos, with year
  captions, drawn at random. Not shown in states 1–5, when the LQ board has the TV.
- **The memories QR slide** (section 5).
- **"Last year's winner"** callout on the TV before betting opens, if open question 1 says
  names are kept.

---

## 11. Backfill of past years [PROPOSED]

- One year page for every past DDM, starting with the first (open question 2).
- Each gets the real Derby result for that year (public record), the date, and whatever
  photos exist. Two are already in the project files: the DDM invite art and the
  CarryBack Derby image.
- Typed and uploaded by hand on the Archive admin page. No import tool.

---

## 12. Open questions

1. **Names.** Record guest names anywhere (draw winners, La Subasta buyers, award winners,
   sign-in)? Earlier, Joey saw no need except a "last year's winner" callout.
2. **Which years?** The first DDM's year, and any skipped years.
3. **Webcam.** Which camera and where it mounts; whether a clip is wanted or stills are
   enough.
4. **Backup target.** Shop PC or the NAS.
5. **Guest vote and sign-in:** build for 2027, or later?

---

## 13. Build order

One single-concern Claude Code prompt each. These follow parent spec items 1–6. Items A1–A5
must be done before the code freeze, or DDM 2027 is recorded no better than past years.

| # | Prompt | For 2027 |
|---|---|---|
| A1 | Archive store and pages: the tables, `/archive`, `/archive/admin` with the basics, highlights and awards; Control Center menu link | Must |
| A2 | Host photo upload and gallery: resize, thumbnails, HEIC, EXIF, hide and delete | Must |
| A3 | Backups: the nightly off-box copy and Export year | Must |
| A4 | Close-out: the snapshots, the betting curve, the `RACE_NIGHT.md` step | Must |
| A5 | Guest photo drop: `/memories`, the approval queue, the QR slide | Must |
| A6 | "DDM Through the Years" slide | Should |
| A7 | Race-state webcam | Should |
| A8 | Best hat vote; sign-in | If open question 5 says so |
| A9 | "Last year's winner" callout | If open question 1 says so |

Backfill (section 11) can start as soon as A2 is on DevPi.
