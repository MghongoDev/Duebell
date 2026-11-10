"""The content security policy and the scripts it allows (issue #49)."""

import hashlib
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"
TEMPLATES = sorted((APP_DIR / "templates").glob("*.html"))
VENDOR = APP_DIR / "static" / "vendor"


def directives(policy: str) -> dict[str, list[str]]:
    parsed = {}
    for part in policy.split(";"):
        words = part.split()
        if words:
            parsed[words[0]] = words[1:]
    return parsed


def test_script_src_allows_only_this_app(client):
    policy = directives(client.get("/healthz").headers["content-security-policy"])
    assert policy["script-src"] == ["'self'"]


def test_policy_names_no_external_hosts(client):
    policy = client.get("/healthz").headers["content-security-policy"]
    assert "http:" not in policy
    assert "https:" not in policy
    for sources in directives(policy).values():
        for source in sources:
            assert source.startswith("'") or source in {"data:"}, source


def test_templates_have_no_inline_script():
    for template in TEMPLATES:
        html = template.read_text()
        for tag in re.findall(r"<script\b[^>]*>", html, flags=re.IGNORECASE):
            if 'type="application/json"' in tag:
                continue
            assert re.search(r"\bsrc=", tag), f"{template.name}: inline script {tag}"
        assert not re.search(r"\son[a-z]+\s*=", html, flags=re.IGNORECASE), template.name
        assert "javascript:" not in html.lower(), template.name


def test_templates_load_scripts_and_styles_from_this_app():
    for template in TEMPLATES:
        html = template.read_text()
        for url in re.findall(r'<(?:script|link)\b[^>]*\b(?:src|href)="([^"]+)"', html):
            assert url.startswith("/static/"), f"{template.name}: {url}"


def test_vendor_files_match_their_recorded_hashes():
    readme = (VENDOR / "README.md").read_text()
    recorded = dict(re.findall(r"^\| `([^`]+)` \|.*`([0-9a-f]{64})` \|$", readme, flags=re.MULTILINE))
    files = sorted(p for p in VENDOR.rglob("*") if p.is_file() and p.name != "README.md")
    assert files
    for path in files:
        name = path.relative_to(VENDOR).as_posix()
        assert name in recorded, f"{name} has no recorded hash"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == recorded[name], name


def test_dashboard_script_is_served(client):
    response = client.get("/static/dashboard.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
