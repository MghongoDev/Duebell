"""The hidden attribute always hides an element; checked in a browser too (issue #63)."""

import re
from pathlib import Path

STYLES = Path(__file__).resolve().parent.parent / "app" / "static" / "styles.css"


def test_the_hidden_attribute_wins_over_any_display_rule():
    css = STYLES.read_text()
    rule = re.search(r"(?m)(?:^|[},/])\s*\[hidden\]\s*(?:,[^{]*)?\{([^}]*)\}", css)
    assert rule, "styles.css needs a global [hidden] rule"
    assert re.search(r"display:\s*none\s*!important", rule.group(1))
