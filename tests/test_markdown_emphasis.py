"""The Drive bios are authored in Markdown; the plugin only wraps paragraphs, so emphasis
must be rendered to HTML in Python before publishing or the asterisks show up literally
(reported live on residency event bodies). See _markdown_emphasis_to_html."""
from outputs.wordpress_events import _markdown_emphasis_to_html as md


def test_bold():
    assert md("**Legends of Classic Rock** rocks") == "<strong>Legends of Classic Rock</strong> rocks"


def test_italic_titles():
    assert md("roles on *Taxi* and *Who*") == "roles on <em>Taxi</em> and <em>Who</em>"


def test_bold_italic_triple():
    assert md("***Navidad*** opens") == "<strong><em>Navidad</em></strong> opens"


def test_adjacent_bold_then_italic_splits():
    # "**Name***Title*" — bold run closes, italic run opens, no space between.
    assert md("**Terry – Vocals***Platinum frontman*") == "<strong>Terry – Vocals</strong><em>Platinum frontman</em>"


def test_multiple_bolds_one_line():
    assert md("**A** **&** **B**") == "<strong>A</strong> <strong>&</strong> <strong>B</strong>"


def test_plain_text_unchanged():
    assert md("no emphasis here") == "no emphasis here"


def test_unbalanced_left_literal():
    # An odd single asterisk has no partner — leave it rather than mangle the text.
    assert md("2 * 3 = 6") == "2 * 3 = 6"
