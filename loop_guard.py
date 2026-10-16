"""Is a model's reply stuck repeating itself? Shared by Athena's own turns and by bots.

In prose, a loop is a phrase restated again and again, often with different filler between the copies, so the
tail only has to have appeared several times. Code is the opposite: repeated lines are normal (the same
`transition: all 0.3s ease;` in every rule, a row of identical table cells), so inside a fenced code block a loop
means the same chunk repeated back to back, many times over, which a model that is really stuck produces and a
stylesheet or markup never does.
"""

_PROSE_WINDOWS = ((40, 3), (18, 5))        # (characters of tail, times it must already have appeared)
_CODE_WINDOW = 18
_CODE_MIN_REPEATS = 80
_CODE_MIN_SPAN = 400


def _in_code_block(text):
    return text.count("```") % 2 == 1


def _prose_loop(text):
    for window, repeats in _PROSE_WINDOWS:
        if len(text) < window * repeats:
            continue
        tail = text[-window:]
        if len(tail.strip()) < window * 0.5:
            continue                      # mostly whitespace: not a meaningful signal
        if text.count(tail) >= repeats:
            return True
    return False


def _code_loop(text):
    if len(text) < _CODE_WINDOW * 3:
        return False
    tail = text[-_CODE_WINDOW:]
    if len(tail.strip()) < _CODE_WINDOW * 0.5:
        return False
    earlier = text.rfind(tail, 0, len(text) - 1)
    if earlier == -1:
        return False
    period = len(text) - _CODE_WINDOW - earlier
    needed = max(_CODE_MIN_REPEATS, -(-_CODE_MIN_SPAN // period))
    if period < 1 or period * needed > len(text):
        return False
    recent = text[-period * needed:]
    return recent[period:] == recent[:-period]


def detect_text_loop(text):
    return _code_loop(text) if _in_code_block(text) else _prose_loop(text)
