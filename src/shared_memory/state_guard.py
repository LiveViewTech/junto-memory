"""State-spec write guard — make silent loss at park visible.

interface:state-spec-write-guard-v0 (backlog_889bbe240666). memory_define_spec
replaces content, so a line an agent omits when re-writing its state spec is
deleted with no trace (learning_8f912797b5eebed8 — e.g. "NEVER `-Recurse`
D:\\Media" vanished from state:server-team 1.1.228 → 1.1.229).

ADVISORY ONLY: report which standing fragments of the previous version appear
nowhere in the new content. Never blocks; the >50% shrink guard in
tools/specs.py is separate and unchanged. Pure functions, no I/O.
"""

import re
from typing import List

# Headings whose content is meant to persist across parks. Status / Next Steps
# churn legitimately every park, so they are deliberately NOT diffed.
STANDING_HEADING = re.compile(
    r"key context|standing|rules|don'?t repeat|gotcha|never|warning", re.IGNORECASE
)
# Only RULE-SHAPED fragments are reported. Replay over every fleet state write
# 05-14→10-13 (5,375 writes): all standing fragments flagged 82% of parks
# (median 3) — Key Context is reworded every park, so that is noise agents
# would learn to skip. Rule-shaped only: 38% of parks, mean 1.07, and it still
# catches the server-team 1.1.229 "NEVER `-Recurse` D:\\Media" loss.
RULE_SHAPED = re.compile(
    r"\b(NEVER|ALWAYS|DON'?T|DO NOT|MUST(?: NOT)?|forbidden|no approval)\b|⚠️|🚨|⛔",
    re.IGNORECASE,
)
MIN_FRAGMENT_CHARS = 12
MAX_REPORTED = 25

_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_LEAD = re.compile(r"^(?:[-*•>]|\d+[.)]|⚠️|\s)+")
_SPLIT = re.compile(r" · |; ")
# Sentence boundary inside a fragment. A Key Context line often carries a
# fact AND a rule ("What's live: run X. Never write a SHA here."); editing the
# fact must not report the untouched rule as dropped.
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z`(⚠🚦])")
_WORD = re.compile(r"[\w@./:-]+")
# A rule counts as KEPT when this share of its words appear in one sentence of
# the new content — a reword, not a loss. Replay over 171 fleet state writes
# 10-13→10-15: exact line matching flagged 43, sentence split + this overlap 17,
# and every remaining hit is a rule actually missing from the new version.
REWORD_OVERLAP = 0.7


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def standing_fragments(content: str) -> List[str]:
    """Fragments (one line split on ' · ' / '; ') under standing headings.

    A standing section runs until the next heading of the same or higher level.
    """
    out: List[str] = []
    level = None  # level of the standing heading we are inside, if any
    for raw in content.splitlines():
        m = _HEADING.match(raw)
        if m:
            depth = len(m.group(1))
            if level is not None and depth <= level:
                level = None
            if level is None and STANDING_HEADING.search(m.group(2)):
                level = depth
            continue
        if level is None:
            continue
        line = _LEAD.sub("", raw).strip()
        for frag in _SPLIT.split(line):
            frag = frag.strip()
            if len(frag) >= MIN_FRAGMENT_CHARS:
                out.append(frag)
    return out


def _words(text: str) -> set:
    return {w.casefold() for w in _WORD.findall(text) if len(w) > 2}


def dropped_standing(old_content: str, new_content: str) -> List[str]:
    """Rule-shaped standing sentences of old_content missing from new_content.

    A sentence counts as kept when it appears (case/whitespace-insensitive)
    anywhere in the new content — so a rule moved to another section is not
    dropped — or when REWORD_OVERLAP of its words appear in one new sentence.
    """
    new_norm = _norm("\n".join(_LEAD.sub("", ln) for ln in new_content.splitlines()))
    new_sentences = [_words(s) for s in _SENTENCE.split(new_norm)]
    seen = set()
    dropped: List[str] = []
    for frag in standing_fragments(old_content):
        for sentence in _SENTENCE.split(frag):
            sentence = sentence.strip()
            if len(sentence) < MIN_FRAGMENT_CHARS or not RULE_SHAPED.search(sentence):
                continue
            key = _norm(sentence)
            if key in seen:
                continue
            seen.add(key)
            if key in new_norm:
                continue
            words = _words(sentence)
            if words and any(len(words & n) / len(words) >= REWORD_OVERLAP for n in new_sentences):
                continue
            dropped.append(sentence)
    return dropped
