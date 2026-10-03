"""Rule suggestions mined from the move journal (V3).

The journal records what fs-organizer *did*. Scans record what it *skipped*.
Together they answer the most useful configuration question a user has:
"which rule should I add next?"

Two signals are surfaced:

- **Skipped extensions.** Files that matched no rule and were not AI-able
  (the ``unknown`` bucket in ``plan_actions``) are the rules you are missing.
  The extension and a representative file name are reported with a
  ready-to-paste config snippet.
- **Journal activity.** Extensions that the organizer actually moved, so the
  user can see which rules are carrying the load (and which categories are
  unused).

Pure reporting: nothing is written, no config is modified. The output is a
dict of suggestions the CLI/dashboard render.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from . import journal
from .mover import plan_actions


def _unknown_by_extension(config, max_files: int = 2000) -> dict[str, dict]:
    """Extensions that matched no rule and are not AI-able, with an example."""
    actions = plan_actions(config, max_files=max_files)
    out: dict[str, dict] = {}
    for row in actions["would_skip_unknown"]:
        path = Path(row["path"])
        ext = path.suffix.lower() or "(none)"
        entry = out.setdefault(ext, {"extension": ext, "count": 0, "example": path.name})
        entry["count"] += 1
    return out


def _journal_activity(config) -> dict[str, int]:
    """Category -> number of moves recorded in the journal."""
    counts: Counter[str] = Counter()
    try:
        entries = journal.read_entries(config.journal.resolved_path())
    except Exception:  # noqa: BLE001 - suggestions must never break a run
        return {}
    for entry in entries:
        category = entry.get("category")
        if isinstance(category, str) and category:
            counts[category] += 1
    return dict(counts)


def suggestions(config, max_files: int = 2000, top: int = 10) -> dict:
    """Suggest rules for skipped extensions + summarize journal activity.

    ``suggested_rules`` is ordered by how many files each extension accounts
    for, so the highest-value rule comes first. ``unused_categories`` lists
    AI-allowed categories the rules never mention and the journal never used —
    a hint that the policy is wider than reality.
    """
    unknown = _unknown_by_extension(config, max_files=max_files)
    ordered = sorted(unknown.values(), key=lambda e: (-e["count"], e["extension"]))
    activity = _journal_activity(config)

    suggested = []
    for entry in ordered[: max(0, top)]:
        ext = entry["extension"]
        # Reuse an existing category whose extension list is closest in spirit:
        # fall back to a neutral "Other" so the snippet is always pasteable.
        suggested.append(
            {
                "extension": ext,
                "count": entry["count"],
                "example": entry["example"],
                "suggested_category": "Other",
                "snippet": {ext: "Other"} if ext != "(none)" else {},
            }
        )

    used = set(activity)
    rule_categories = set(config.target_rules.values())
    unused = sorted(
        c for c in config.ai.allowed_subfolders
        if c.strip() and c.strip() not in used and c.strip() not in rule_categories
    )

    return {
        "skipped_unknown_total": sum(entry["count"] for entry in ordered),
        "suggested_rules": suggested,
        "journal_moves_by_category": activity,
        "unused_categories": unused,
        "journal_enabled": bool(getattr(config.journal, "enabled", False)),
    }


def render_suggestions(data: dict) -> str:
    """Human-readable rendering of :func:`suggestions` output."""
    lines: list[str] = []
    add = lines.append
    if not data["suggested_rules"]:
        add("  no unmatched files found — every candidate is covered by a rule or AI")
    else:
        add(
            f"  {data['skipped_unknown_total']} file(s) match no rule and are not AI-able; "
            f"top suggestions:"
        )
        for s in data["suggested_rules"]:
            add(
                f"    {s['extension']:<8} {s['count']:>4} file(s)  "
                f"(e.g. {s['example']})  -> add rule {s['snippet']}"
            )
    if data.get("journal_moves_by_category"):
        add("  moves recorded in the journal (by category):")
        for cat, n in sorted(
            data["journal_moves_by_category"].items(), key=lambda kv: -kv[1]
        ):
            add(f"    {cat}: {n}")
    elif not data.get("journal_enabled"):
        add("  journal disabled — enable it to see which rules are doing the work")
    if data.get("unused_categories"):
        add(f"  AI-allowed categories never used or ruled: {', '.join(data['unused_categories'])}")
    return "\n".join(lines)


__all__ = ["render_suggestions", "suggestions"]
