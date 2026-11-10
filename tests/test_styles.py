"""The hidden attribute always hides an element (issue #63).

A class rule such as `.muted { display: block }` outranks the browser's own
`[hidden]` style, so `element.hidden = true` left the grid's loading line on
screen. One global rule in the app's stylesheet settles it for every element.
This guards the rule's presence; the behaviour itself was checked in a browser.
"""

import re
from pathlib import Path

STYLES = Path(__file__).resolve().parent.parent / "app" / "static" / "styles.css"


def test_the_hidden_attribute_wins_over_any_display_rule():
    css = STYLES.read_text()
    rule = re.search(r"(?m)^\[hidden\]\s*\{([^}]*)\}", css)
    assert rule, "styles.css needs a global [hidden] rule"
    assert re.search(r"display:\s*none\s*!important", rule.group(1))
