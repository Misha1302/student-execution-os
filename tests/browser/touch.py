"""Phone touch-target audit shared by the browser suites.

Every visible tappable control in the workspace and the tab bar must be at least 44 CSS
px in both directions on a phone (WCAG 2.5.5, Android/iOS guidance). Controls folded
inside a closed <details> are not visible and are not measured.
"""

TOUCH_AUDIT_JS = """() => [...document.querySelectorAll('#workspace button, #workspace a[href], #workspace summary, #workspace [role=button], .tabbar button')]
    .filter((el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
      return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && !el.closest('details:not([open]) > :not(summary)'); })
    .map((el) => { const r = el.getBoundingClientRect();
      return { h: Math.round(r.height), w: Math.round(r.width), text: (el.getAttribute('aria-label') || el.innerText || el.dataset.action || el.dataset.nav || '').trim().slice(0, 40) }; })
    .filter((x) => x.h < 44 || x.w < 44)"""
