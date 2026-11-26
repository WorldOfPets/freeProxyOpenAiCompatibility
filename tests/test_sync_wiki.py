from __future__ import annotations

from pathlib import Path

import pytest

from scripts.sync_wiki import MARKER_PREFIX, _rewrite_link, sync


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_sync_copies_pages_and_rewrites_links(tmp_path):
    docs = tmp_path / "docs"
    _write(
        docs / "api.md",
        "# API\n\nSee [auth](auth.md#login) and [example](../examples/opencode.jsonc).\n",
    )
    _write(docs / "auth.md", "# Auth\n\nBack to [api](api.md).\n")
    wiki = tmp_path / "wiki"
    _write(
        wiki / "stale.md",
        f"{MARKER_PREFIX} from docs/stale.md; edit the source instead -->\nold\n",
    )
    _write(wiki / "manual.md", "# Manual\n")

    result = sync(docs, wiki, repo_url="https://github.com/o/r/", branch="master")

    assert set(result.written) == {"api.md", "auth.md", "Home.md", "_Sidebar.md"}
    assert result.removed == ("stale.md",)
    assert not (wiki / "stale.md").exists()
    assert _read(wiki / "manual.md") == "# Manual\n"

    api = _read(wiki / "api.md")
    assert api.startswith(MARKER_PREFIX)
    assert "[auth](auth#login)" in api
    assert "https://github.com/o/r/blob/master/examples/opencode.jsonc" in api

    auth = _read(wiki / "auth.md")
    assert "[api](api)" in auth

    home = _read(wiki / "Home.md")
    assert "[API](api)" in home
    assert "[Auth](auth)" in home
    assert "https://github.com/o/r/tree/master/docs" in home

    sidebar = _read(wiki / "_Sidebar.md")
    assert "[FreeProxyAPI](Home)" in sidebar
    assert "- [API](api)" in sidebar


def test_sync_is_idempotent(tmp_path):
    docs = tmp_path / "docs"
    _write(docs / "api.md", "# API\n\nHello.\n")
    wiki = tmp_path / "wiki"

    sync(docs, wiki, repo_url="https://github.com/o/r")
    before = {path.name: _read(path) for path in wiki.glob("*.md")}
    sync(docs, wiki, repo_url="https://github.com/o/r")
    after = {path.name: _read(path) for path in wiki.glob("*.md")}

    assert before == after


def test_rewrite_link_external_and_unknown_targets():
    pages = {"auth.md": "auth"}
    assert (
        _rewrite_link(
            "https://example.com/x",
            pages=pages,
            repo_url="https://github.com/o/r",
            branch="master",
        )
        == "https://example.com/x"
    )
    assert (
        _rewrite_link(
            "#section",
            pages=pages,
            repo_url="https://github.com/o/r",
            branch="master",
        )
        == "#section"
    )
    assert (
        _rewrite_link(
            "unknown.md#part",
            pages=pages,
            repo_url="https://github.com/o/r",
            branch="master",
        )
        == "https://github.com/o/r/blob/master/docs/unknown.md#part"
    )


def test_sync_rejects_reserved_wiki_page_names(tmp_path):
    docs = tmp_path / "docs"
    _write(docs / "Home.md", "# Home\n")
    with pytest.raises(ValueError, match="reserved"):
        sync(docs, tmp_path / "wiki", repo_url="https://github.com/o/r")


def test_sync_requires_docs_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        sync(
            tmp_path / "missing",
            tmp_path / "wiki",
            repo_url="https://github.com/o/r",
        )
