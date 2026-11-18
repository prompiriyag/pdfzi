# Security policy

## Supported versions

Security fixes go into the latest release only. pdfzi is young (0.x), so please update
before reporting.

## Reporting a problem

Please do not open a public issue for a security problem. Use GitHub's private reporting
instead: open the repository's **Security** tab and choose **Report a vulnerability**.

A useful report says which pdfzi version and operating system you used, what you ran, what
happened and what you expected. If a file triggers it, describe how to make one rather than
attaching a document with real contents.

pdfzi is maintained by one person in their spare time, so replies can take a while. Every
report is read, and security problems are fixed before other work. Once a fix is released,
the report can be made public, with credit if you want it.

## What counts

pdfzi runs only on your computer and never connects to the network. The main risk is a
crafted PDF or image that you open with it. Examples of what we want to hear about:

- a file that makes pdfzi run code, write outside the folder it was asked to write to, or
  overwrite a file without `--force`
- a file that makes pdfzi hang or use huge amounts of memory instead of stopping with a
  message
- text from a file (names, metadata) reaching your terminal as control codes
- `clean` leaving behind metadata that it says it removes, or `extract` / `delete` / `split`
  leaving content of removed pages inside the result

Out of scope:

- `clean` is not redaction. It removes metadata, not anything visible on the pages, and it
  does not remove scripts, attachments or form contents.
- Weaknesses in a PDF's own protection when the password is known or weak.
- Problems in the libraries pdfzi uses (pikepdf/qpdf, PDFium, Pillow ...) should go to those
  projects. If pdfzi uses them in an unsafe way, that is in scope.
