"""Guard the documentation set against rot.

The docs are grouped by audience and cross-reference each other heavily, so a
renamed file or a retitled section silently breaks navigation. These tests are
cheap and catch exactly that: every relative link and every heading anchor must
resolve, every audience page must exist, and the index must actually list them.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"

#: The audience pages docs/README.md routes readers to. Renaming one without
#: updating the index is the failure this guards.
AUDIENCE_PAGES = (
    "for-members.md",
    "moderator-guide.md",
    "running-optimus.md",
)

_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.MULTILINE)


def _markdown_files() -> list[Path]:
    return sorted([*REPO_ROOT.glob("*.md"), *DOCS.rglob("*.md")])


def _anchors(text: str) -> set[str]:
    """GitHub-style heading slugs: lowercase, punctuation dropped, spaces to dashes."""
    return {
        re.sub(r"[^a-z0-9\- ]", "", heading.lower()).replace(" ", "-")
        for heading in _HEADING.findall(text)
    }


def test_every_relative_doc_link_resolves() -> None:
    broken: list[str] = []
    for path in _markdown_files():
        text = path.read_text()
        for target in _LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            file_part = target.partition("#")[0]
            if not file_part:
                continue
            if not (path.parent / file_part).resolve().exists():
                broken.append(f"{path.relative_to(REPO_ROOT)} -> {target}")
    assert not broken, "broken relative links:\n" + "\n".join(broken)


def test_every_doc_anchor_resolves() -> None:
    """A ``file.md#section`` link must point at a heading that exists."""
    broken: list[str] = []
    for path in _markdown_files():
        text = path.read_text()
        own_anchors = _anchors(text)
        for target in _LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            file_part, _, fragment = target.partition("#")
            if not fragment:
                continue
            if not file_part:
                if fragment not in own_anchors:
                    broken.append(f"{path.relative_to(REPO_ROOT)} -> #{fragment}")
                continue
            resolved = (path.parent / file_part).resolve()
            if resolved.suffix != ".md" or not resolved.exists():
                continue
            if fragment not in _anchors(resolved.read_text()):
                broken.append(f"{path.relative_to(REPO_ROOT)} -> {target}")
    assert not broken, "broken heading anchors:\n" + "\n".join(broken)


def test_audience_pages_exist_and_are_indexed() -> None:
    index = (DOCS / "README.md").read_text()
    for page in AUDIENCE_PAGES:
        assert (DOCS / page).is_file(), f"docs/{page} is missing"
        assert page in index, f"docs/README.md no longer links docs/{page}"


def test_root_readme_points_at_the_docs_index() -> None:
    assert "docs/README.md" in (REPO_ROOT / "README.md").read_text()
