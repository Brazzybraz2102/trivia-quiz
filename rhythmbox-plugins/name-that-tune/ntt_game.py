# ntt_game.py - game rules for the Name That Tune Rhythmbox plugin
#
# Kept free of GTK/Rhythmbox imports so it can be unit tested on its own.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import random
from collections import namedtuple

ROUNDS = 10
SNIPPET_SECONDS = 15
MAX_POINTS = 1000
MIN_POINTS = 100
CHOICES = 4
MIN_TRACK_SECONDS = 30

# ref is whatever the caller needs to play the track (a RhythmDBEntry).
Track = namedtuple("Track", "title artist album duration ref")
Question = namedtuple("Question", "track kind choices answer start")

KINDS = ("title", "artist")


def _norm(text):
    return " ".join(text.casefold().split())


def usable_tracks(tracks, unknown="Unknown"):
    """Drop tracks that are too short or are missing a title or artist."""
    bad = {"", _norm(unknown)}
    return [t for t in tracks
            if _norm(t.title or "") not in bad
            and _norm(t.artist or "") not in bad
            and (t.duration or 0) >= MIN_TRACK_SECONDS]


def distinct_values(tracks, kind):
    """Map normalised value -> display value for one field."""
    values = {}
    for t in tracks:
        value = getattr(t, kind)
        values.setdefault(_norm(value), value)
    return values


def playable_kinds(tracks):
    """Question kinds that have enough different answers to offer 4 choices."""
    return [k for k in KINDS if len(distinct_values(tracks, k)) >= CHOICES]


def snippet_start(duration, rng=random):
    """Pick a start point that skips the intro and leaves room for the snippet."""
    latest = max(0, duration - SNIPPET_SECONDS - 1)
    low = min(int(duration * 0.15), latest)
    high = min(int(duration * 0.6), latest)
    return rng.randint(low, max(low, high))


def points_for(elapsed):
    """Faster answers score more: MAX_POINTS at 0s down to MIN_POINTS."""
    if elapsed <= 0:
        return MAX_POINTS
    if elapsed >= SNIPPET_SECONDS:
        return MIN_POINTS
    span = MAX_POINTS - MIN_POINTS
    return int(round(MAX_POINTS - span * elapsed / SNIPPET_SECONDS))


def build_question(tracks, track, kind, rng=random):
    answer = getattr(track, kind)
    pool = distinct_values(tracks, kind)
    pool.pop(_norm(answer), None)
    wrong = rng.sample(sorted(pool.values()), CHOICES - 1)
    choices = wrong + [answer]
    rng.shuffle(choices)
    return Question(track, kind, choices, answer,
                    snippet_start(track.duration, rng))


class Game:
    def __init__(self, tracks, rounds=ROUNDS, unknown="Unknown", rng=None):
        self.rng = rng or random.Random()
        self.tracks = usable_tracks(tracks, unknown)
        self.kinds = playable_kinds(self.tracks)
        if not self.kinds:
            raise ValueError(
                "Your library needs at least %d songs with different titles "
                "or artists to play." % CHOICES)
        self.rounds = min(rounds, len(self.tracks))
        self.round = 0
        self.score = 0
        self.streak = 0
        self.best_streak = 0
        self.correct = 0
        self.history = []
        self.question = None
        self._unused = list(self.tracks)
        self.rng.shuffle(self._unused)

    @property
    def finished(self):
        return self.round >= self.rounds and self.question is None

    def next_question(self):
        if self.round >= self.rounds:
            self.question = None
            return None
        self.round += 1
        track = self._unused.pop()
        kind = self.rng.choice(self.kinds)
        self.question = build_question(self.tracks, track, kind, self.rng)
        return self.question

    def answer(self, choice, elapsed):
        """Score a choice (None means time ran out). Returns points earned."""
        q = self.question
        correct = choice is not None and _norm(choice) == _norm(q.answer)
        points = points_for(elapsed) if correct else 0
        if correct:
            self.correct += 1
            self.streak += 1
            self.best_streak = max(self.best_streak, self.streak)
            # Small bonus for keeping a streak going.
            points += 50 * (self.streak - 1)
        else:
            self.streak = 0
        self.score += points
        self.history.append((q, choice, correct, points))
        self.question = None
        return correct, points
