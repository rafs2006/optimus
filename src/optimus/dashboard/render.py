"""Server-rendered HTML for the dashboard: no JavaScript, no external assets.

Every dynamic value passes through :func:`html.escape` before it reaches a
page. The dashboard renders *records about* images (ids, hashes, distances,
verdicts) and never the images themselves, so a page can't embed scam content.
"""

from __future__ import annotations

import html
from collections.abc import Iterable, Sequence

from optimus.dashboard.queries import DayActivity

_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #14151a; color: #e6e6ea;
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: #8ab4ff; text-decoration: none; }
a:hover { text-decoration: underline; }
header { display: flex; align-items: baseline; gap: 1rem; padding: 0.9rem 1.4rem;
  background: #1c1e26; border-bottom: 1px solid #2c2f3a; flex-wrap: wrap; }
header .brand { font-weight: 700; font-size: 1.05rem; color: #fff; }
header .who { margin-left: auto; color: #9aa0ae; font-size: 0.85rem; }
main { max-width: 70rem; margin: 0 auto; padding: 1.4rem; }
h1 { font-size: 1.35rem; margin: 0.2rem 0 1rem; }
h2 { font-size: 1.05rem; margin: 1.6rem 0 0.6rem; color: #c7cad4; }
table { border-collapse: collapse; width: 100%; font-size: 0.88rem; }
th, td { text-align: left; padding: 0.45rem 0.7rem; border-bottom: 1px solid #262933;
  vertical-align: top; }
th { color: #9aa0ae; font-weight: 600; white-space: nowrap; }
tr:hover td { background: #1a1c24; }
code { background: #22242e; padding: 0.1rem 0.35rem; border-radius: 4px;
  font-size: 0.82rem; word-break: break-all; }
.cards { display: flex; gap: 0.8rem; flex-wrap: wrap; margin: 0.8rem 0 0.4rem; }
.card { background: #1c1e26; border: 1px solid #2c2f3a; border-radius: 8px;
  padding: 0.7rem 1rem; min-width: 8.5rem; }
.card .num { font-size: 1.45rem; font-weight: 700; }
.card .lbl { color: #9aa0ae; font-size: 0.8rem; }
.badge { display: inline-block; padding: 0.05rem 0.5rem; border-radius: 99px;
  font-size: 0.78rem; font-weight: 600; }
.badge.clean { background: #12351f; color: #6fdc8f; }
.badge.scam { background: #3d1416; color: #ff8085; }
.badge.ambiguous { background: #3a2c10; color: #f0c05a; }
.badge.other { background: #262933; color: #9aa0ae; }
.chart { display: flex; align-items: flex-end; gap: 3px; height: 120px;
  padding: 0.6rem 0 0.2rem; }
.chart .col { flex: 1; display: flex; flex-direction: column; justify-content: flex-end;
  height: 100%; min-width: 6px; }
.chart .seg-flagged { background: #d9535a; border-radius: 2px 2px 0 0; }
.chart .seg-clean { background: #3d6fb4; }
.chart .col:hover .seg-clean { background: #5a8cd1; }
.axis { display: flex; gap: 3px; color: #6b7080; font-size: 0.68rem; }
.axis span { flex: 1; overflow: hidden; text-align: center; white-space: nowrap; }
.filters { display: flex; gap: 0.6rem; flex-wrap: wrap; margin: 0.6rem 0 1rem;
  align-items: center; font-size: 0.85rem; }
.filters input, .filters select { background: #22242e; color: #e6e6ea;
  border: 1px solid #2c2f3a; border-radius: 6px; padding: 0.3rem 0.55rem; }
.filters button { background: #3d6fb4; color: #fff; border: 0; border-radius: 6px;
  padding: 0.35rem 0.9rem; cursor: pointer; }
.muted { color: #9aa0ae; }
.empty { color: #6b7080; padding: 1.2rem 0; }
.login { display: inline-block; margin-top: 1rem; background: #5865f2; color: #fff;
  padding: 0.55rem 1.2rem; border-radius: 8px; font-weight: 600; }
.tabs { display: flex; gap: 0.9rem; margin: 0 0 1rem; font-size: 0.9rem; }
dl.kv { display: grid; grid-template-columns: max-content 1fr; gap: 0.3rem 1.2rem;
  font-size: 0.9rem; }
dl.kv dt { color: #9aa0ae; }
dl.kv dd { margin: 0; }
"""


def esc(value: object) -> str:
    """HTML-escape any value's string form."""
    return html.escape(str(value), quote=True)


def page(title: str, body: str, *, user: str | None = None, nav: str = "") -> str:
    """Full HTML document with the shared header and styles."""
    who = ""
    if user:
        who = f'<span class="who">{esc(user)} · <a href="/dash/logout">log out</a></span>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{esc(title)} · Optimus</title><style>{_STYLE}</style></head><body>"
        f'<header><span class="brand"><a href="/dash">Optimus</a></span>{nav}{who}</header>'
        f"<main><h1>{esc(title)}</h1>{body}</main></body></html>"
    )


def table(headers: Sequence[str], rows: Iterable[Sequence[str]], *, empty: str) -> str:
    """A table from pre-escaped cell HTML; shows ``empty`` when there are no rows.

    Callers pass cells that are already safe HTML (built with :func:`esc` /
    the badge helpers), which lets a cell contain a link or badge markup.
    """
    body_rows = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    if not body_rows:
        return f'<p class="empty">{esc(empty)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body_rows}</tbody></table>"


def verdict_badge(verdict: str) -> str:
    """Colored pill for a verdict string."""
    cls = verdict if verdict in {"clean", "scam", "ambiguous"} else "other"
    return f'<span class="badge {cls}">{esc(verdict)}</span>'


def stat_cards(stats: Sequence[tuple[str, object]]) -> str:
    """Row of number cards, e.g. scans / flagged / actions in a window."""
    cards = "".join(
        f'<div class="card"><div class="num">{esc(num)}</div>'
        f'<div class="lbl">{esc(label)}</div></div>'
        for label, num in stats
    )
    return f'<div class="cards">{cards}</div>'


def activity_chart(days: Sequence[DayActivity]) -> str:
    """Stacked bar chart of daily scans: clean (blue) under flagged (red).

    Pure CSS flexbox — each day is a column whose segment heights are
    percentages of the window's busiest day.
    """
    peak = max((d.total for d in days), default=0)
    if peak == 0:
        return '<p class="empty">No scans in this window yet.</p>'
    cols: list[str] = []
    for d in days:
        clean_pct = d.clean / peak * 100
        flagged_pct = d.flagged / peak * 100
        title = f"{d.day}: {d.total} scans ({d.flagged} flagged)"
        cols.append(
            f'<div class="col" title="{esc(title)}">'
            f'<div class="seg-flagged" style="height:{flagged_pct:.1f}%"></div>'
            f'<div class="seg-clean" style="height:{clean_pct:.1f}%"></div></div>'
        )
    # Label roughly weekly ticks to keep the axis readable at 30 days.
    step = max(1, len(days) // 6)
    labels = "".join(
        f"<span>{esc(d.day[5:]) if i % step == 0 else ''}</span>" for i, d in enumerate(days)
    )
    return f'<div class="chart">{"".join(cols)}</div><div class="axis">{labels}</div>'
