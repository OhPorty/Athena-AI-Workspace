import pathlib

import pytest

from loop_guard import detect_text_loop

ROOT = pathlib.Path(__file__).resolve().parent.parent


def old_detector(text):
    """What Athena used before: the tail only had to have appeared before, anywhere, with no regard for code."""
    def tail_repeats(window, repeats):
        if len(text) < window * repeats:
            return False
        tail = text[-window:]
        return len(tail.strip()) >= window * 0.5 and text.count(tail) >= repeats
    return tail_repeats(40, 3) or tail_repeats(18, 5)


def first_cut(text, step=7):
    for i in range(step, len(text) + step, step):
        if detect_text_loop(text[:i]):
            return i
    return None


STYLESHEET = "Here's the CSS:\n\n```css\n" + "".join(
    f".site-header .item-{n} {{ color: #fff; padding: 8px 12px; transition: all 0.3s ease; border-radius: 4px; }}\n" for n in range(30)) + "```\n"


def test_an_ordinary_stylesheet_is_never_cut_off_mid_block():
    assert old_detector(STYLESHEET[:400]) is True                    # this is exactly what used to stop generation
    assert first_cut(STYLESHEET) is None


@pytest.mark.parametrize("path", ["frontend/src/styles/base.css", "bots.py", "frontend/index.html", "main.py", "frontend/src/app.js", "tests/test_research.py"])
def test_real_code_streams_through_untouched(path):
    code = (ROOT / path).read_text()[:30000]
    assert first_cut("```\n" + code, step=37) is None, path


def test_many_identical_markup_rows_are_fine():
    board = "```html\n<div class=\"board\">\n" + '  <div class="cell"></div>\n' * 64 + "</div>\n```"
    assert first_cut(board) is None


def test_a_code_loop_that_never_ends_is_still_caught():
    stuck = "```css\n.a { color: red; }\n" + "    margin: 0;\n" * 400
    cut = first_cut(stuck)
    assert cut is not None and cut < len(stuck)
    assert first_cut("```python\n" + "x = 1\n" * 500) is not None
    assert first_cut("```\n" + "-" * 3000) is not None


def test_prose_loops_are_still_caught():
    assert first_cut("I need to check the file again. " * 8) is not None
    filler = "".join(f"Let me think about this carefully. {w} " for w in ["Hmm.", "Wait.", "Okay.", "Right.", "So."])
    assert first_cut(filler * 2) is not None


def test_normal_prose_and_prose_after_code_are_not_flagged():
    paragraph = ("The header now has a dark background and a cyan underline. The logo glows when you hover it, and the active page "
                 "gets a purple marker. On mobile the navigation collapses into a hamburger button. ") * 1
    assert first_cut(paragraph) is None
    assert first_cut(STYLESHEET + "\nPaste that into the Custom HTML block and the header will pick it up.") is None


def test_an_unfinished_fence_counts_as_code():
    assert first_cut("Here you go:\n```css\n" + STYLESHEET.split("```css\n")[1].split("```")[0]) is None
