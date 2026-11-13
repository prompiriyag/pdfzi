# Changelog

All notable changes to pdfzi are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

First version.

### Added
- Terminal UI (red theme) with a menu, file picker with a path bar and paste support,
  ordering screen, settings form, progress screen and a scrollable result screen.
  All keys are listed in the footer of every screen.
- Command line with the same tools: `compress`, `merge`, `split`, `extract`, `delete`,
  `rotate`, `img2pdf`, `pdf2img`, `lock`, `unlock`, `clean`, `info`. Several files and
  patterns such as `*.pdf` are accepted; `--json` prints machine-readable results.
- Compress levels `lossless`, `medium` (170 dpi, JPEG q80) and `high` (120 dpi, q60),
  `--grayscale`, and `--target` (a plain number means MB; `KB`, `MB`, `GB`, `B` also work).
- Warnings when a file carries a digital signature or claims PDF/A, because changing it
  breaks the signature or the conformance.
- `lock` with AES-256; `unlock` with the document's password.

### Safety
- No network code anywhere; checked by an automated test.
- Originals are never overwritten: results get new names unless `--force` is given.
- Results are written to a temporary file, verified, then moved into place.
- Compress never returns a file that is not at least 1 % smaller than the original.
