//! Team attachment preparation (ADR-0123, plan phase R7, workstream W4b): the worker the `shimpz-team prepare`
//! subcommand runs inside the networkless helper container, for one image (decoded within pixel limits, oriented,
//! resized, re-encoded without metadata) or one PDF (bounded pre-pass, then `PDFium`). It replaces `prepare/`.
