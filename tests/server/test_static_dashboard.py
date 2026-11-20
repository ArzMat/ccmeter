"""The dashboard files shipped in the package: complete, self-contained, CSP-compatible."""

from __future__ import annotations

import re

from ccmeter.server.app import STATIC_DIR

PAGE = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
SCRIPT = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
STYLES = (STATIC_DIR / "styles.css").read_text(encoding="utf-8")
SVG_NAMESPACE = "www.w3.org/2000/svg"


def test_every_file_the_page_references_is_shipped() -> None:
    references = re.findall(r'(?:src|href)="([^"#:]+)"', PAGE)
    references += re.findall(r'from "\./([^"]+)"', SCRIPT)
    references += re.findall(r'url\("([^"]+)"\)', STYLES)

    assert references
    for reference in references:
        assert (STATIC_DIR / reference.removeprefix("./")).is_file(), reference


def test_the_page_has_no_inline_script_or_style_for_the_content_security_policy() -> None:
    """CSP is `script-src 'self'; style-src 'self'`: inline code would be blocked."""
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", PAGE, flags=re.S)

    assert all(body.strip() == "" for body in scripts)
    assert all("src=" in tag for tag in re.findall(r"<script\b[^>]*>", PAGE))
    assert "<style" not in PAGE
    assert " style=" not in PAGE


def test_nothing_is_loaded_from_other_origins() -> None:
    for name, text in (("index.html", PAGE), ("app.js", SCRIPT), ("styles.css", STYLES)):
        urls = [url for url in re.findall(r"https?://[^\s\"')]+", text) if SVG_NAMESPACE not in url]
        assert urls == [], name
    assert "../api/dashboard/" in SCRIPT


def test_bundled_third_party_files_carry_their_licenses() -> None:
    for shipped, license_file in (
        ("vendor/preact.module.js", "vendor/preact.LICENSE.txt"),
        ("vendor/htm.module.js", "vendor/htm.LICENSE.txt"),
        ("fonts/SpaceGrotesk-latin.woff2", "fonts/SpaceGrotesk.OFL.txt"),
        ("fonts/JetBrainsMono-latin.woff2", "fonts/JetBrainsMono.OFL.txt"),
    ):
        assert (STATIC_DIR / shipped).is_file(), shipped
        assert (STATIC_DIR / license_file).stat().st_size > 500, license_file
