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


# --- Bullet markers are list syntax, not emphasis (event 11225, 2026-08-06) -------------
# Kiss The Sky's Drive bio is a "*" bulleted list under Visual/Sound/Authentic headings.
# Pairing bullet 1's marker with bullet 2's opened an <em> in one paragraph and closed it
# in the next; the plugin splits the description on blank lines into one Gutenberg block
# each, so the editor flagged both blocks as "unexpected or invalid content".

KTS_BIO = (
    "Visual\n"
    "* The ONLY Hendrix tribute with a full cast of look-a-like performers.\n"
    "\n"
    "* The ONLY Hendrix tribute wearing custom made 60's wardrobes.\n"
    "\n"
    "* The ONLY Hendrix tribute with stage sets of replica gear.\n"
    "Sound\n"
    "* The ONLY Hendrix tribute gaining the classic HENDRIX sound LIVE:"
)


def test_bullet_markers_are_not_emphasis():
    assert md(KTS_BIO) == KTS_BIO


def test_dash_and_plus_bullets_pass_through():
    assert md("- first\n- second\n+ third") == "- first\n- second\n+ third"


def test_emphasis_inside_a_bullet_still_converts():
    # The marker is skipped, not the rest of the line.
    assert md("* He starred in *Taxi* for years") == "* He starred in <em>Taxi</em> for years"


def test_emphasis_never_spans_a_paragraph_break():
    # The tags would land in two different Gutenberg blocks — leave the asterisks literal.
    assert md("open *here\n\nand close* there") == "open *here\n\nand close* there"


def test_no_block_has_unbalanced_tags():
    # The property that actually matters: every blank-line-delimited chunk (one Gutenberg
    # paragraph block) must close every tag it opens.
    for chunk in md(KTS_BIO).split("\n\n"):
        assert chunk.count("<em>") == chunk.count("</em>")
        assert chunk.count("<strong>") == chunk.count("</strong>")
