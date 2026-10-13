"""Structural fingerprint of a page, diffed night over night.

The fingerprint is the set of interactive elements described by their
*stable* attributes (tag, role, type, name, data-qa, data-testid, aria-label,
placeholder, non-generated id). Text and generated ids are left out so the
members list changing by one person does not count as UI drift. A non-empty
diff is an early warning: the selectors still match tonight, but the service
has shipped new markup around them.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

_JS = """
() => {
  const sel = 'a,button,input,select,textarea,[role],[data-qa],[data-testid],[aria-label]';
  const out = new Set();
  const generated = /\\d{3,}|[0-9a-f]{8}/i;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (!r.width && !r.height) continue;
    const parts = [el.tagName.toLowerCase()];
    for (const a of ['role','type','name','data-qa','data-testid','aria-label','placeholder']) {
      const v = el.getAttribute(a);
      if (v && !generated.test(v)) parts.push(a + '=' + v.slice(0, 40));
    }
    if (el.id && !generated.test(el.id)) parts.push('id=' + el.id);
    if (parts.length > 1) out.add(parts.join('|'));
  }
  return Array.from(out).sort();
}
"""


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, slug: str, step: str) -> Path:
        safe = re.sub(r"\W+", "_", step).strip("_")
        return self.root / slug / f"{safe}.json"

    @staticmethod
    def snapshot(page) -> list[str]:
        sig = page.evaluate(_JS)
        return list(sig) if isinstance(sig, list) else []

    def record(self, slug: str, step: str, page) -> dict[str, list[str]] | None:
        """Snapshot `page`, diff against the stored one, store the new one.

        Returns {"added": [...], "removed": [...]} or None on the first run.
        """
        sig = self.snapshot(page)
        path = self._path(slug, step)
        previous: list[str] | None = None
        if path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8")).get("sig")
            except (OSError, ValueError):
                previous = None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "when": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "url": page.url,
                    "sig": sig,
                },
                indent=1,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        if previous is None:
            return None
        before, after = set(previous), set(sig)
        return {
            "added": sorted(after - before)[:40],
            "removed": sorted(before - after)[:40],
        }
