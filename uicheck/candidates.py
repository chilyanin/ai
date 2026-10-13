"""Extract repair candidates from the live page.

`collect()` returns the interactive elements under `root` (a Page or a
Locator) as compact dicts - tag/role/text/attributes plus two selectors
generated in the page:

* `uniq`    - matches exactly this element within the root (data-qa >
              data-testid > id > name > aria-label > stable classes >
              tag:has-text > nth-of-type path)
* `generic` - the same minus anything instance-specific, for row/container
              keys that must match every row, not one.

With `contains_text`, only elements whose text contains it (and their
nearest container ancestors) are returned; with `container=True` leaf
controls are dropped in favour of their row-like ancestors.
"""
from __future__ import annotations

from typing import Any

# `root` is bound by Playwright: for a Locator it is the element handle, for a
# Page we pass `document`.
_COLLECT_JS = r"""
(root, args) => {
  const {containsText, container, limit} = args;
  const generated = /\d{3,}|[0-9a-f]{8}/i;
  const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/[^\w-]/g, '\\$&');
  const q = (s) => '"' + String(s).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  const attr = (el, a) => { const v = el.getAttribute(a); return v ? `[${a}=${q(v)}]` : null; };
  const unique = (sel) => { try { return root.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  const visible = (el) => { const r = el.getBoundingClientRect(); return (r.width > 0 || r.height > 0); };
  const text = (el) => ((el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ')).slice(0, 80);
  const rowLike = (el) => {
    const t = el.tagName.toLowerCase();
    const role = el.getAttribute('role') || '';
    return t === 'tr' || t === 'li' || role === 'row' || role === 'listitem' ||
      /\b(row|item|member|user|person)\b/i.test(el.className || '') ||
      /(row|item|member|user|person)/i.test(el.getAttribute('data-qa') || el.getAttribute('data-testid') || '');
  };

  function build(el) {
    const tag = el.tagName.toLowerCase();
    const tries = [];
    for (const a of ['data-qa', 'data-testid', 'data-test', 'name', 'aria-label']) {
      const v = el.getAttribute(a);
      if (v && !generated.test(v)) { const s = attr(el, a); tries.push(tag + s); tries.push(s); }
    }
    if (el.id && !generated.test(el.id)) tries.push('#' + esc(el.id));
    const cls = Array.from(el.classList || []).filter(c => !generated.test(c) && !/^(css|sc|jss|styled|emotion)-/i.test(c) && c.length < 40).slice(0, 3);
    if (cls.length) tries.push(tag + cls.map(c => '.' + esc(c)).join(''));
    const role = el.getAttribute('role');
    if (role) tries.push(`${tag}[role=${q(role)}]`);
    if (tag === 'input' && el.type) tries.push(`input[type=${q(el.type)}]`);
    if (el.getAttribute('placeholder')) tries.push(`${tag}[placeholder=${q(el.getAttribute('placeholder'))}]`);
    if (el.getAttribute('title')) tries.push(`${tag}[title=${q(el.getAttribute('title'))}]`);
    if (tag === 'a' && el.getAttribute('href') && !generated.test(el.getAttribute('href'))) tries.push(`a[href=${q(el.getAttribute('href'))}]`);

    const generic = tries.find(t => !t.startsWith('#')) || (role ? `[role=${q(role)}]` : tag);
    let uniq = tries.find(unique) || null;
    const t = text(el);
    if (!uniq && t && ['button', 'a', 'li', 'span', 'div', 'label', 'td', 'th'].includes(tag)) {
      uniq = `${tag}:has-text(${q(t.slice(0, 40))})`;   // Playwright pseudo, verified by the caller
    }
    if (!uniq) {
      const path = []; let cur = el; let depth = 0;
      while (cur && cur.nodeType === 1 && cur !== root && depth < 4) {
        const p = cur.parentElement; let s = cur.tagName.toLowerCase();
        if (p) { const sib = Array.from(p.children).filter(c => c.tagName === cur.tagName); if (sib.length > 1) s += `:nth-of-type(${sib.indexOf(cur) + 1})`; }
        path.unshift(s); cur = p; depth++;
      }
      uniq = path.join(' > ');
    }
    return {uniq, generic};
  }

  const sel = 'a,button,input,select,textarea,[role],[data-qa],[data-testid],[aria-label],[onclick],tr,li,[role="row"]';
  let els = Array.from(root.querySelectorAll(sel)).filter(visible);
  if (containsText) {
    const needle = containsText.toLowerCase();
    const hits = els.filter(el => (el.innerText || el.value || '').toLowerCase().includes(needle));
    const set = new Set();
    for (const h of hits) {
      let cur = h; let depth = 0;
      while (cur && cur !== root && depth < 6) { set.add(cur); cur = cur.parentElement; depth++; }
    }
    els = Array.from(set).filter(visible);
    // Prefer the smallest containers that still carry the text: drop ancestors
    // whose text is much larger than the needle's row.
    els = els.filter(el => (el.innerText || '').length < 2000);
  }
  if (container) els = els.filter(rowLike);
  const seen = new Set();
  const out = [];
  for (const el of els) {
    const {uniq, generic} = build(el);
    const key = container ? generic : uniq;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute('role') || '',
      type: el.getAttribute('type') || '',
      text: text(el),
      aria: (el.getAttribute('aria-label') || '').slice(0, 60),
      placeholder: (el.getAttribute('placeholder') || '').slice(0, 60),
      name: (el.getAttribute('name') || '').slice(0, 60),
      dataqa: (el.getAttribute('data-qa') || el.getAttribute('data-testid') || el.getAttribute('data-test') || '').slice(0, 60),
      title: (el.getAttribute('title') || '').slice(0, 60),
      href: (el.getAttribute('href') || '').slice(0, 60),
      cls: Array.from(el.classList || []).slice(0, 6).join(' '),
      textlen: (el.innerText || '').length,
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      uniq, generic,
    });
    if (out.length >= limit) break;
  }
  return out;
}
"""


def collect(root, *, contains_text: str | None = None, container: bool = False, limit: int = 160) -> list[dict[str, Any]]:
    """Candidates under `root` (Page or Locator). See module docstring."""
    args = {"containsText": contains_text or "", "container": container, "limit": limit}
    if hasattr(root, "evaluate") and not hasattr(root, "goto"):
        # Locator: the element itself becomes `root` in the JS.
        out = root.evaluate(_COLLECT_JS, args)
    else:
        out = root.evaluate(f"(args) => ({_COLLECT_JS})(document, args)", args)
    cands = [c for c in (out or []) if isinstance(c, dict)]
    for i, c in enumerate(cands):
        c["i"] = i
    return cands


def describe(c: dict[str, Any]) -> str:
    """One-line rendering of a candidate for prompts and logs."""
    bits = [f"<{c['tag']}"]
    for k in ("role", "type", "dataqa", "aria", "name", "placeholder", "title", "cls"):
        if c.get(k):
            bits.append(f'{k}="{c[k]}"')
    if c.get("disabled"):
        bits.append("disabled")
    head = " ".join(bits) + ">"
    txt = f' text="{c["text"]}"' if c.get("text") else ""
    return f"[{c['i']}] {head}{txt}  selector: {c['uniq']}"
