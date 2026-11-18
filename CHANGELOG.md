# Changelog

All notable changes to pdfzi are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-18

First version.

### Added
- Terminal UI (red theme) with a menu, file picker with a path bar and paste support,
  ordering screen, settings form, progress screen and a scrollable result screen.
  All keys are listed in the footer of every screen. The mouse works too: a click ticks a
  file, and buttons under the file list and the ordering screen go on, move or remove
  files. The settings of tools that take page numbers show how many pages each file has.
- Command line with the same tools: `compress`, `merge`, `split`, `extract`, `delete`,
  `rotate`, `img2pdf`, `pdf2img`, `lock`, `unlock`, `clean`, `info`. Several files and
  patterns such as `*.pdf` are accepted; `--json` prints machine-readable results.
- Compress levels `lossless`, `low` (220 dpi, JPEG q85), `medium` (180 dpi, q80) and
  `high` (140 dpi, q70), `--grayscale`, and `--target` (a plain number means MB; `KB`,
  `MB`, `GB`, `B` also work). Images are written as progressive JPEG; an image is only
  replaced if it gets at least 20 % smaller, and a JPEG already of the chosen quality or
  lower is left alone instead of being made blockier.
- `--target` searches a 0-100 strength scale by halving (at most 9 tries) for the
  gentlest settings that fit, then keeps some images at gentler settings or as they were
  to land close to the target. It squeezes images at most to 70 dpi and JPEG quality 45,
  where text in scans is still readable; a target that needs more is reported as not
  reached.
- Warnings when a file carries a digital signature or claims PDF/A, because changing it
  breaks the signature or the conformance.
- `lock` with AES-256; `unlock` with the document's password.

### Safety
- No network code anywhere; checked by an automated test.
- Originals are never overwritten: results get new names unless `--force` is given.
- Results are written to a temporary file, verified, then moved into place. Without
  `--force`, a file that appears under the same name while pdfzi is working is not
  replaced either: the result takes the next free name.
- On macOS and Linux a result is never readable by more people than its sources (a
  private original gives a private result), and it stays private while it is written.
- Compress never returns a file that is not at least 1 % smaller than the original.
- `extract`, `delete` and `split` keep nothing of the pages they leave out: no form field
  values, comments or images of those pages ride along through links from the kept pages.
- `clean` also removes the author names and dates of comments, and gives the result a new
  file identifier. It cleans photos however their compression is written (`/DCTDecode`,
  `[/DCTDecode]`, or wrapped in `/FlateDecode`) and says so when it meets a photo it
  cannot safely unpack, instead of reporting "no metadata".
- When `split` or `pdf2img` stops halfway, the new files of that run are removed, but
  files that had already replaced an older one with `--force` are kept and listed.
- File names and PDF metadata are shown with control characters made harmless, so a
  crafted file cannot send commands to your terminal.
- Crafted files (images that inflate to gigabytes, endlessly nested drawings, malformed
  image entries) are skipped or refused with a clear message instead of stopping pdfzi.
  Drawings built to take hours to measure make `compress` leave the images as they are
  (and say why) rather than hang; measuring can also be cancelled.
- `clean` unpacks at most 100 MB to reach a wrapped photo, whatever width and height the
  file claims for it; a larger one is reported as not checked.
- `compress` also leaves the images as they are (and says why) when it cannot follow every
  drawing: forms nested more than 32 levels deep, a form that draws itself, or damaged
  page content. It no longer goes by the uses it could see, which could shrink an image
  too far for a larger use it missed.
- Ctrl+C during `split` or `pdf2img` on the command line now also lists the files that
  were kept, then prints "Interrupted." (exit status 130).
- Several inputs with the same name from different folders (`a/report.pdf`,
  `b/report.pdf`) no longer write the same result: the second gets `report-rotated-2.pdf`
  (or its own `-2` folder for `split` and `pdf2img`). With `--force` the second used to
  replace the first.
- A file whose XMP metadata would unpack to more than 16 MB is refused with a message.
  Real files carry a few KB; such a file is made to use up memory, and every result would
  have carried the unpacked metadata.
- Page or form drawing instructions that would unpack to more than 64 MB make `extract`,
  `delete` and `split` refuse the file, and `compress` skip removing unused resources.
  `compress` reads at most 4 MB of instructions per page or form (256 MB per file) to
  measure images; past that it leaves the images as they are and says why.
- Damaged files (page sizes or rotation that are not numbers, broken bookmarks, forms
  with broken resources, names that are not valid text, pages PDFium cannot load) now give
  a clear message or are handled, instead of "This is a bug in pdfzi".
- Pillow's warning about very large pictures is no longer printed over the terminal UI.
- The terminal UI uses passwords exactly as typed, spaces included, like the command line.
