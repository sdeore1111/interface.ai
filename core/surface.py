"""
SurfaceAdapter: the seam between "how we perceive/act on a surface" and
"the recorded flow" (see REPORT.md, Heterogeneity & multi-tenant).

Both the discovery agent (agent/loop.py) and the deterministic replay
engine (replay/engine.py) talk to the target application only through this
class -- neither ever touches Playwright directly. That is the design
answer to Section 3.7's "surface abstraction" question: everything above
this line (the agent loop, the artifact schema, the replay engine) is
surface-agnostic. Everything below this line is Playwright-specific and
would be swapped for a different adapter (a native desktop
accessibility-API adapter, a screenshot+coordinate adapter) to support a
new surface type, without changing the artifact schema or the replay
engine's control flow.

Perception is accessibility-tree-first, not screenshot/pixel-first. Roles
and accessible names survive `<table>`-for-layout, missing test ids, and
even a lot of markup churn, which is exactly the "no clean DOM" case the
brief calls out as the common one. CSS/XPath candidates exist in the
locator fallback chain for the cases where the accessible tree is not
enough, but the walk order always tries role-based first.

Every real action (navigate/click/type/select) is preceded by a call into
the SafetyGate; this class owns that choke point so neither caller can
forget it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from playwright.sync_api import Page, TimeoutError as PWTimeoutError

from artifacts.schema import Locator, LocatorCandidate, LocatorStrategy
from safety.policy import SafetyGate


@dataclass
class GroundedElement:
    """
    One actionable control found by the DOM scan, already carrying a real,
    replay-portable Locator (candidate chain) -- not a reference to our
    scan-time marker attribute. The LLM only ever sees `key`/`role`/`label`;
    the moment it acts on `key`, the agent loop resolves it to `.locator`
    and that Locator (not the key) is what gets written into the artifact.
    """
    key: str
    role: str
    label: str
    locator: "Locator"


@dataclass
class Observation:
    """What the agent (or a human debugging a log) sees at one point in time."""
    url: str
    title: str
    http_status: Optional[int]
    elements: List[GroundedElement] = field(default_factory=list)
    visible_text_excerpt: str = ""  # capped excerpt of visible text, for outcome-probe text matching

    def to_prompt_text(self, max_elements: int = 40) -> str:
        lines = [f"URL: {self.url}", f"Title: {self.title}"]
        if self.http_status is not None:
            lines.append(f"HTTP status: {self.http_status}")
        lines.append("Actionable elements (key: [role] label):")
        for el in self.elements[:max_elements]:
            lines.append(f"  - {el.key}: [{el.role}] {el.label!r}")
        if self.visible_text_excerpt:
            lines.append("Visible text excerpt:")
            lines.append(self.visible_text_excerpt[:1500])
        return "\n".join(lines)


class LocatorResolutionError(Exception):
    pass


def _xpath_literal(s: str) -> str:
    """Safely embed a string containing quotes into an XPath expression."""
    if '"' not in s:
        return f'"{s}"'
    if "'" not in s:
        return f"'{s}'"
    parts = s.split('"')
    return "concat(" + ', \'"\', '.join(f'"{p}"' for p in parts) + ")"


class PlaywrightSurface:
    """Concrete SurfaceAdapter for browser-based (web) targets."""

    def __init__(self, page: Page, safety_gate: SafetyGate):
        self.page = page
        self.safety_gate = safety_gate
        self.last_http_status: Optional[int] = None
        self.page.on("response", self._track_main_response)

    def _track_main_response(self, response):
        try:
            if response.url == self.page.url or response.request.is_navigation_request():
                self.last_http_status = response.status
        except Exception:
            pass

    # ---- perception --------------------------------------------------
    #
    # Grounding strategy, layered (most to least stable):
    #   1. Real id/name attribute on the control itself       -> CSS candidate
    #   2. <label for=...> association                        -> LABEL candidate
    #   3. Accessible role, when the browser exposes one       -> ROLE candidate
    #   4. Visible text (for onclick-span "fake buttons" and
    #      table-adjacent form fields with no <label> at all)  -> TEXT/XPATH candidate
    # A legacy page built from tables with onclick handlers (this project's
    # mock app) rarely has (1) or (2) for its "buttons", so (4) is not a
    # last resort here -- it's the only thing that actually works, which is
    # the point: the scan always produces >= 2 independently-failing
    # candidates per element so replay has real fallback, not padding.

    _SCAN_JS = r"""
() => {
  function isVisible(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && el.offsetParent !== null;
  }
  function labelForField(el) {
    if (el.labels && el.labels.length && el.labels[0].innerText.trim()) {
      return el.labels[0].innerText.trim();
    }
    const td = el.closest('td');
    if (td && td.previousElementSibling) {
      const t = td.previousElementSibling.innerText.trim();
      if (t) return t;
    }
    return el.placeholder || el.name || el.id || '';
  }
  const out = [];
  let counter = 0;
  document.querySelectorAll('input, select, textarea').forEach(el => {
    if (el.type === 'hidden' || el.type === 'submit' || !isVisible(el)) return;
    const key = 'e' + (counter++);
    el.setAttribute('data-cua-key', key);
    out.push({
      key, tag: el.tagName.toLowerCase(), type: el.type || '',
      id: el.id || '', name: el.name || '',
      role: el.tagName.toLowerCase() === 'select' ? 'select'
            : (el.type === 'password' ? 'password_field' : 'textbox'),
      label: labelForField(el),
    });
  });
  document.querySelectorAll('button, a[href], [onclick], input[type=submit]').forEach(el => {
    if (!isVisible(el)) return;
    const text = (el.innerText || el.value || '').trim();
    if (!text) return;
    const key = 'e' + (counter++);
    el.setAttribute('data-cua-key', key);
    out.push({
      key, tag: el.tagName.toLowerCase(), type: el.type || '',
      id: el.id || '', name: el.name || '',
      role: 'button', label: text,
    });
  });
  return out;
}
"""

    def scan_actionable_elements(self) -> "dict[str, GroundedElement]":
        raw = self.page.evaluate(self._SCAN_JS)
        result: "dict[str, GroundedElement]" = {}
        for item in raw:
            candidates: List[LocatorCandidate] = []
            if item["id"]:
                candidates.append(LocatorCandidate(
                    strategy=LocatorStrategy.CSS, value=f"#{item['id']}",
                    robustness_note="Stable as long as the element keeps this id; ids rarely change on legacy pages.",
                ))
            if item["name"]:
                candidates.append(LocatorCandidate(
                    strategy=LocatorStrategy.CSS, value=f"[name=\"{item['name']}\"]",
                    robustness_note="Survives id churn; form field 'name' attributes are functional, so rarely renamed.",
                ))
            if item["role"] == "button" or item["tag"] in ("a", "button"):
                candidates.append(LocatorCandidate(
                    strategy=LocatorStrategy.TEXT, value=item["label"],
                    robustness_note="Legacy control has no id/name (e.g. onclick-span fake button); "
                                     "visible text is the only thing a human would also rely on. "
                                     "Breaks if the label copy changes.",
                ))
            else:
                candidates.append(LocatorCandidate(
                    strategy=LocatorStrategy.XPATH,
                    value=f"//td[contains(normalize-space(.),{_xpath_literal(item['label'])})]"
                          f"/following-sibling::td//{item['tag']}",
                    robustness_note="Table-adjacency fallback for a field with no id/name/label element; "
                                     "breaks if row layout is restructured, but survives styling/id changes.",
                ))
            if item["role"] in ("button", "textbox", "select", "password_field"):
                candidates.append(LocatorCandidate(
                    strategy=LocatorStrategy.ROLE,
                    value="button" if item["role"] == "button" else "textbox",
                    role_name=item["label"],
                    robustness_note="Accessible-role match; cheap extra fallback, works if the browser "
                                     "computes an accessible name even without explicit ARIA.",
                ))
            loc = Locator(description=f"{item['role']} '{item['label']}'", candidates=candidates)
            result[item["key"]] = GroundedElement(
                key=item["key"], role=item["role"], label=item["label"], locator=loc,
            )
        return result

    def observe(self) -> Observation:
        grounded = self.scan_actionable_elements()
        try:
            body_text = self.page.inner_text("body")
        except Exception:
            body_text = ""
        return Observation(
            url=self.page.url,
            title=self.page.title(),
            http_status=self.last_http_status,
            elements=list(grounded.values()),
            visible_text_excerpt=body_text[:3000],
        )

    def get_text_for_probe(self) -> str:
        try:
            return self.page.inner_text("body")
        except Exception:
            return ""

    # ---- locator resolution -------------------------------------------

    def resolve(self, locator: Locator, timeout_ms: int = 8000):
        """Try each candidate in order; return (playwright_locator, candidate_used)."""
        last_error = None
        for cand in locator.candidates:
            try:
                pw_loc = self._to_playwright_locator(cand)
                pw_loc.wait_for(state="visible", timeout=timeout_ms)
                if pw_loc.count() == 1:
                    return pw_loc, cand
                # If ambiguous, narrow to first exact match rather than failing outright.
                if pw_loc.count() > 1:
                    return pw_loc.first, cand
            except (PWTimeoutError, Exception) as e:  # noqa: BLE001 - broad on purpose, we fall through
                last_error = e
                continue
        raise LocatorResolutionError(
            f"No candidate resolved for locator '{locator.description}'. Last error: {last_error}"
        )

    def _to_playwright_locator(self, cand: LocatorCandidate):
        if cand.strategy == LocatorStrategy.ROLE:
            return self.page.get_by_role(cand.value, name=cand.role_name or "")
        if cand.strategy == LocatorStrategy.LABEL:
            return self.page.get_by_label(cand.value)
        if cand.strategy == LocatorStrategy.TEXT:
            # Scoped to plausible clickable tags with exact text/value match, so a
            # coincidental text match elsewhere on the page (e.g. an <h3> heading
            # that repeats the button's label) doesn't get picked over the real
            # control -- a real failure mode we hit while building this adapter.
            xp = (
                "//*[(self::button or self::a or self::span or self::input or self::div)"
                f" and (normalize-space(text())={_xpath_literal(cand.value)}"
                f" or @value={_xpath_literal(cand.value)})]"
            )
            return self.page.locator(f"xpath={xp}")
        if cand.strategy == LocatorStrategy.CSS:
            return self.page.locator(cand.value)
        if cand.strategy == LocatorStrategy.XPATH:
            return self.page.locator(f"xpath={cand.value}")
        raise LocatorResolutionError(f"Strategy not supported by web adapter: {cand.strategy}")

    # ---- actions (each gated by policy) --------------------------------

    def navigate(self, url: str):
        self.safety_gate.check_navigation(url)
        self.safety_gate.check_action_type("navigate")
        self.page.goto(url, wait_until="domcontentloaded")
        self.last_http_status = None  # will be repopulated by response listener

    def click(self, locator: Locator, timeout_ms: int = 8000):
        self.safety_gate.check_action_type("click")
        pw_loc, cand = self.resolve(locator, timeout_ms)
        pw_loc.click(timeout=timeout_ms)
        return cand

    def type_text(self, locator: Locator, text: str, timeout_ms: int = 8000):
        self.safety_gate.check_action_type("type")
        pw_loc, cand = self.resolve(locator, timeout_ms)
        pw_loc.fill(text, timeout=timeout_ms)
        return cand

    def select(self, locator: Locator, value: str, timeout_ms: int = 8000):
        self.safety_gate.check_action_type("select")
        pw_loc, cand = self.resolve(locator, timeout_ms)
        pw_loc.select_option(value, timeout=timeout_ms)
        return cand

    def wait_for(self, locator: Locator, timeout_ms: int = 8000):
        self.safety_gate.check_action_type("wait_for")
        return self.resolve(locator, timeout_ms)

    def extract_text(self, locator: Locator, timeout_ms: int = 8000) -> str:
        self.safety_gate.check_action_type("extract")
        pw_loc, cand = self.resolve(locator, timeout_ms)
        return pw_loc.inner_text(timeout=timeout_ms)

    def screenshot(self, path: str):
        self.page.screenshot(path=path, full_page=True)

    def dom_snapshot(self) -> str:
        return self.page.content()
