"""The Drive bios carry structure — section labels, bullet lists, band rosters, gear lists —
but the plugin's tour_calendar_clean_text unwraps every single newline inside a paragraph
into flowing prose. That is right for a hard-wrapped .txt file and wrong for these bios: it
glued Kiss The Sky's labels to its lists ("…classic concerts. Sound * The ONLY Hendrix…")
and ran its gear list together, which is what the team saw as "random words" in the event
body (reported 2026-08-06 against event 11225).

_render_bio_markdown protects that structure by rendering it to HTML — <br>, bullet glyphs,
<strong>/<em> — before the text ever reaches the plugin. A <br> is not a newline, so it
survives the unwrap, wp_kses_post, and Gutenberg's block validation.
"""
import re

from outputs.wordpress_events import (
    _bullet_markers_to_glyphs,
    _preserve_structural_breaks,
    _render_bio_markdown,
    _section_labels_to_bold,
)


def _plugin_unwrap(text: str) -> str:
    """What tour_calendar_clean_text does to one paragraph: join every soft newline."""
    return "\n\n".join(
        re.sub(r"\s*\n\s*", " ", para) for para in text.split("\n\n")
    )


# --- Structural breaks are kept -----------------------------------------------------

def test_label_line_keeps_its_break():
    # "Visual" is a section label, not the start of the bullet's sentence.
    out = _preserve_structural_breaks("Visual\n• The ONLY Hendrix tribute with a full cast.")
    assert out == "Visual<br>• The ONLY Hendrix tribute with a full cast."


def test_short_list_lines_each_keep_their_break():
    # The gear list: no punctuation, no bullets, just short lines. Each break is real.
    gear = "MARSHALL AMP WALLED STACKS\nLUDWIG DRUMS\nFENDER STRATOCASTER GUITARS"
    assert _preserve_structural_breaks(gear) == (
        "MARSHALL AMP WALLED STACKS<br>LUDWIG DRUMS<br>FENDER STRATOCASTER GUITARS"
    )


def test_short_lines_do_not_accumulate_into_a_join():
    # Judged per line-pair: a run of short lines must not add up past the wrap threshold
    # and start joining ("FENDER STRATOCASTER GUITARS FX: VOX, …").
    assert "\n" not in _preserve_structural_breaks(
        "MARSHALL AMP WALLED STACKS\nLUDWIG DRUMS\nFENDER STRATOCASTER GUITARS\n"
        "FX: VOX, WAH-WAH, UNIVIBE, OCTAVIO, FUZZ FACE & more!"
    )


def test_sentence_end_keeps_its_break():
    out = _preserve_structural_breaks("…accurate to Jimi's most classic concerts.\nSound")
    assert out.endswith("concerts.<br>Sound")


def test_bold_lead_in_opens_a_new_unit():
    # The band-roster bios are a run of "**Name** (credits)" lines; the previous line is
    # long and unpunctuated, so only the bold lead-in marks the next one as a new item.
    roster = (
        "<strong>Mitch Perry</strong> (The Edgar Winter Group, Ratt, Cher, Lita Ford)\n"
        "<strong>Sean McNabb</strong> (Dokken, Great White, Montrose, House of Lords)"
    )
    assert "<br><strong>Sean McNabb</strong>" in _preserve_structural_breaks(roster)


def test_strong_tags_do_not_inflate_a_short_line_past_the_threshold():
    # Measured on visible text: 51 visible chars, ~90 with markup. Must still break.
    line = "<strong>Jake Faun – Lead Guitar</strong> <strong>&</strong> <strong>Vocals</strong>"
    out = _preserve_structural_breaks(line + "\nDavid Lee Roth, Selena Gomez, Winger")
    assert "<br>David Lee Roth" in out


# --- Genuine soft wraps are still joined --------------------------------------------

def test_mid_sentence_runover_is_left_for_the_plugin_to_join():
    # Back 2 Mac's bio — the one real hard wrap across all 23 acts. A long line that
    # doesn't finish its sentence, continued by ordinary prose.
    wrapped = (
        "As Back 2 Mac continues to impress with higher and higher ticket sales at major "
        "venues, they are rapidly becoming the Fleetwood Mac concert of\n"
        "choice for major clubs and performing arts venues East of the Mississippi."
    )
    out = _preserve_structural_breaks(wrapped)
    assert "<br>" not in out
    assert "concert of choice for major clubs" in _plugin_unwrap(out)


def test_paragraph_breaks_are_untouched():
    assert _preserve_structural_breaks("One.\n\nTwo.") == "One.\n\nTwo."


# --- Bullet glyphs -------------------------------------------------------------------

def test_asterisk_bullet_becomes_a_glyph():
    assert _bullet_markers_to_glyphs("* first\n* second") == "• first\n• second"


def test_dash_and_plus_bullets_become_glyphs():
    assert _bullet_markers_to_glyphs("- one\n+ two") == "• one\n• two"


def test_prose_asterisk_is_not_a_bullet():
    assert _bullet_markers_to_glyphs("2 * 3 = 6") == "2 * 3 = 6"


# --- Section labels ------------------------------------------------------------------

def test_label_heading_a_list_is_bolded():
    assert _section_labels_to_bold("Visual\n• A full cast of look-a-likes.") == (
        "<strong>Visual</strong>\n• A full cast of look-a-likes."
    )


def test_label_not_followed_by_a_list_is_left_alone():
    # Requiring the list below it is what keeps ordinary short lines out.
    assert _section_labels_to_bold("Visual\nJust some prose.") == "Visual\nJust some prose."


def test_a_bullet_is_never_a_label():
    assert _section_labels_to_bold("• one\n• two") == "• one\n• two"


def test_a_sentence_is_never_a_label():
    assert _section_labels_to_bold("That was the show.\n• one") == "That was the show.\n• one"


def test_a_long_line_is_never_a_label():
    long_line = "This runs well past what any section label would ever plausibly be, so it stays prose"
    assert _section_labels_to_bold(long_line + "\n• one") == long_line + "\n• one"


def test_an_already_bold_line_is_not_double_wrapped():
    line = "<strong>Terry Ilous – Lead Vocals</strong>"
    assert _section_labels_to_bold(line + "\n• Great White") == line + "\n• Great White"


# --- End to end ----------------------------------------------------------------------

KTS_BIO = (
    "Visual\n"
    "* The ONLY Hendrix tribute with a full cast of look-a-like performers.\n"
    "\n"
    "* The ONLY Hendrix tribute with stage sets of replica gear.\n"
    "Sound\n"
    "* The ONLY Hendrix tribute gaining the classic HENDRIX sound LIVE:\n"
    "\n"
    "MARSHALL AMP WALLED STACKS\n"
    "LUDWIG DRUMS\n"
    "FENDER STRATOCASTER GUITARS"
)


def test_kts_bio_survives_the_plugin_unwrap_with_structure_intact():
    published = _plugin_unwrap(_render_bio_markdown(KTS_BIO))
    # The labels the team saw stranded mid-sentence now start their own line.
    assert "<strong>Visual</strong><br>•" in published
    # The exact glue the team reported: "…replica gear. Sound * The ONLY Hendrix tribute…"
    # The label now also opens its own paragraph, so it has space above it (2026-08-14).
    assert "replica gear.\n\n<strong>Sound</strong><br>• The ONLY Hendrix tribute gaining" in published
    assert "MARSHALL AMP WALLED STACKS<br>LUDWIG DRUMS<br>FENDER STRATOCASTER GUITARS" in published
    # No literal Markdown leaks through.
    assert "*" not in re.sub(r"<[^>]+>", "", published)


def test_every_published_block_has_balanced_tags():
    # An unbalanced tag across a blank line is exactly what Gutenberg reports as
    # "Block contains unexpected or invalid content".
    for chunk in _plugin_unwrap(_render_bio_markdown(KTS_BIO)).split("\n\n"):
        for tag in ("em", "strong"):
            assert chunk.count("<%s>" % tag) == chunk.count("</%s>" % tag)
