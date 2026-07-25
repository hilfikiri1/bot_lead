# Brand assets

Place your company assets here.

## Logo

Put your logo as `logo.png` in this directory (path configurable via
`BRAND_LOGO_PATH`). Recommended: a transparent PNG, at least 480 px wide.

**If `logo.png` is missing, the catalog still renders** — a text logo with the
`BRAND_NAME` value is used instead, so the app never crashes.

## Fonts

The Docker image installs `fonts-noto` and `fonts-noto-cjk`, which cover both
Cyrillic and Chinese characters. If you render PDFs outside Docker, install
equivalent fonts on your host (see the project README). To bundle custom fonts,
drop the `.ttf`/`.otf` files in `fonts/` and reference them from `catalog.css`
via `@font-face`.
