"""The Python transliterator must say exactly what the browser's does.

`crm/yozuv.py` exists so the server can answer "would these two strings read the
same?" — the question that decides whether a new marka is a lookalike of one already
on the books. That answer is worthless if it drifts from `static/js/yozuv.js`, which
is what the operator actually sees, so the tables are read straight out of the JS
here and compared. Edit one side alone and this fails.
"""
import re

import pytest
from crm.yozuv import DIGRAPHS, KEEP, LETTERS, PAIRS, reads_the_same, to_kiril

JS = "static/js/yozuv.js"


def _source():
    with open(JS, encoding="utf-8") as handle:
        return handle.read()


def _js_block(name):
    """The text between `var <name> = ` and its closing bracket."""
    match = re.search(rf"var {name} = ([\[{{].*?[\]}}]);", _source(), re.S)
    assert match, f"{name} topilmadi — {JS} qayta yozilganmi?"
    return match.group(1)


def test_the_single_letters_match():
    js = dict(re.findall(r"(\w+): \"(.)\"", _js_block("LETTERS")))
    assert js == LETTERS


def test_the_pairs_match():
    js = dict(re.findall(r'\["(\w\w)", "(.)"\]', _js_block("PAIRS")))
    assert js == PAIRS


def test_the_digraphs_match():
    js = {latin: (small, big) for latin, small, big
          in re.findall(r'\["(\w)", "(.)", "(.)"\]', _js_block("DIGRAPHS"))}
    assert js == DIGRAPHS


def test_the_kept_words_match():
    js = set(re.findall(r"(\w+): 1", _js_block("KEEP")))
    assert js == KEEP


def test_the_apostrophes_match():
    match = re.search(r'var APOSTROPHE = "(.*?)";', _source())
    from crm.yozuv import APOSTROPHE
    assert match.group(1) == APOSTROPHE


@pytest.mark.parametrize("latin,kiril", [
    ("i 1561", "и 1561"),          # the pair that started this
    ("eshik", "эшик"),             # э opens a word
    ("ber", "бер"),                # е everywhere after it
    ("sho'r", "шўр"),              # digraph inside a pair's reach
    ("yo'l", "йўл"),               # the o' wins over the yo
    ("ma'lumot", "маълумот"),      # a lone apostrophe is the hard sign
    ("Shu", "Шу"),                 # a capitalised pair keeps its one capital
    ("SHU", "SHU"),                # ...but two capitals in a row read as a code
    ("LLDPE", "LLDPE"),            # two capitals — a code, left alone
    ("campaund", "campaund"),      # a bare c — not an Uzbek Latin word
    ("GranulaLog", "GranulaLog"),  # the app's own name
    ("и 1561", "и 1561"),          # already kiril — idempotent
])
def test_it_reads_the_way_the_browser_writes_it(latin, kiril):
    assert to_kiril(latin) == kiril


class TestReadsTheSame:
    def test_the_two_markalar_collide(self):
        assert reads_the_same("i 1561", "и 1561")

    def test_a_name_is_not_a_lookalike_of_itself(self):
        assert not reads_the_same("i 1561", "i 1561")

    def test_genuinely_different_markalar_do_not_collide(self):
        assert not reads_the_same("i 1561", "i 1562")
        assert not reads_the_same("2102 repak", "2102 campaund")

    def test_blank_is_not_a_collision(self):
        assert not reads_the_same("", "")
        assert not reads_the_same(None, "i 1561")
