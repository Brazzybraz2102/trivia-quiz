# Unit tests for the GTK-free parts of the Rhythmbox plugins.
# Run with: python3 -m unittest discover -s rhythmbox-plugins/tests

import os
import random
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "name-that-tune"))
sys.path.insert(0, os.path.join(HERE, "..", "now-playing"))

import ntt_game
import np_lyrics
from ntt_game import Game, Track


def library(n=12):
    return [Track("Song %d" % i, "Artist %d" % (i % 6), "Album", 200, i) for i in range(n)]


class GameTests(unittest.TestCase):
    def test_filters_unusable_tracks(self):
        tracks = [Track("A", "X", "", 200, 1),
                  Track("", "X", "", 200, 2),
                  Track("B", "Unknown", "", 200, 3),
                  Track("C", "X", "", 10, 4)]
        self.assertEqual([t.ref for t in ntt_game.usable_tracks(tracks)], [1])

    def test_intros_are_excluded(self):
        titles = ["Intro", "Intro (Live)", "The Intro", "INTRODUCTION", "Album intro", "Outro", "Hello"]
        tracks = [Track(t, "X", "", 200, t) for t in titles]
        self.assertEqual([t.ref for t in ntt_game.usable_tracks(tracks)], ["Outro", "Hello"])

    def test_dni_rules(self):
        tracks = [Track("Song A", "Queen", "Jazz", 200, 1, 0, "Rock"),
                  Track("Song B", "Queens of the Stone Age", "Rated R", 200, 2, 0, "Rock"),
                  Track("Jingle Bells", "Choir", "Holiday", 200, 3, 0, "Christmas"),
                  Track("Song C", "Adele", "21", 200, 4, 0, "Pop")]
        keep = lambda dni: [t.ref for t in ntt_game.usable_tracks(tracks, dni=dni)]
        # "is" matches the whole value only, ignoring case.
        self.assertEqual(keep([{"field": "artist", "match": "is", "text": "queen"}]), [2, 3, 4])
        self.assertEqual(keep([{"field": "artist", "match": "contains", "text": "queen"}]), [3, 4])
        self.assertEqual(keep([{"field": "genre", "match": "is", "text": "Christmas"}]), [1, 2, 4])
        self.assertEqual(keep([{"field": "album", "match": "contains", "text": "rated"}]), [1, 3, 4])
        self.assertEqual(keep([{"field": "artist", "match": "is", "text": "  "}]), [1, 2, 3, 4])
        self.assertEqual(keep([]), [1, 2, 3, 4])

    def test_exclude_applies_to_rest_of_game(self):
        game = Game(library(), rounds=10, rng=random.Random(6))
        game.answer(game.next_question().answer, 1)
        game.exclude_upcoming({"field": "artist", "match": "is", "text": "artist 1"})
        artists = []
        while game.next_question():
            artists.append(game.question.track.artist)
            game.answer(None, 15)
        self.assertNotIn("Artist 1", artists)
        # Rounds shrink if there aren't enough songs left.
        game = Game(library(6), rounds=6, rng=random.Random(7))
        game.next_question()
        game.exclude_upcoming({"field": "title", "match": "contains", "text": "song"})
        self.assertEqual(game.rounds, 1)

    def test_dni_counted_in_error(self):
        tracks = [t._replace(artist="Nobody") for t in library()]
        with self.assertRaisesRegex(ValueError, "12 songs are on your Do Not Include list"):
            Game(tracks, dni=[{"field": "artist", "match": "is", "text": "nobody"}])

    def test_question_has_four_distinct_choices_including_answer(self):
        rng = random.Random(1)
        tracks = library()
        for kind in ntt_game.KINDS:
            for track in tracks:
                q = ntt_game.build_question(tracks, track, kind, rng)
                self.assertEqual(len(q.choices), 4)
                self.assertEqual(len({c.casefold() for c in q.choices}), 4)
                self.assertIn(getattr(track, kind), q.choices)

    def test_artist_questions_need_four_artists(self):
        tracks = [Track("S%d" % i, "Artist %d" % (i % 2), "", 200, i) for i in range(8)]
        self.assertEqual(ntt_game.playable_kinds(tracks), ["title"])

    def test_too_small_library_raises(self):
        with self.assertRaises(ValueError):
            Game(library(3))

    def test_snippet_fits_inside_song(self):
        rng = random.Random(2)
        for duration in (30, 31, 60, 200, 900):
            for _ in range(50):
                start = ntt_game.snippet_start(duration, rng)
                self.assertGreaterEqual(start, 0)
                self.assertLessEqual(start + ntt_game.SNIPPET_SECONDS, duration)

    def test_points_drop_with_time(self):
        self.assertEqual(ntt_game.points_for(0), ntt_game.MAX_POINTS)
        self.assertEqual(ntt_game.points_for(99), ntt_game.MIN_POINTS)
        self.assertGreater(ntt_game.points_for(2), ntt_game.points_for(8))

    def test_full_game_scoring_and_no_repeats(self):
        game = Game(library(), rounds=5, rng=random.Random(3))
        seen = set()
        while True:
            q = game.next_question()
            if q is None:
                break
            seen.add(q.track.ref)
            game.answer(q.answer, 0)
        self.assertTrue(game.finished)
        self.assertEqual(len(seen), 5)
        self.assertEqual(game.correct, 5)
        self.assertEqual(game.best_streak, 5)
        # 5 x 1000 plus streak bonuses 0+50+100+150+200
        self.assertEqual(game.score, 5000 + 500)

    def test_wrong_and_timeout_reset_streak(self):
        game = Game(library(), rounds=3, rng=random.Random(4))
        q = game.next_question()
        game.answer(q.answer, 1)
        q = game.next_question()
        wrong = next(c for c in q.choices if c != q.answer)
        self.assertEqual(game.answer(wrong, 1), (False, 0))
        self.assertEqual(game.streak, 0)
        game.next_question()
        self.assertEqual(game.answer(None, 15), (False, 0))

    def test_low_rated_songs_are_skipped(self):
        tracks = [t._replace(rating=r) for t, r in zip(library(), [0, 1, 2, 3, 4, 5] * 2)]
        kept = {t.ref for t in ntt_game.usable_tracks(tracks)}
        self.assertEqual(kept, {t.ref for t in tracks if t.rating in (0, 3, 4, 5)})
        # 0 turns skipping off; a higher threshold skips more.
        self.assertEqual(len(ntt_game.usable_tracks(tracks, skip_rating=0)), 12)
        self.assertEqual(len(ntt_game.usable_tracks(tracks, skip_rating=3)), 6)

    def test_skipped_songs_never_asked(self):
        tracks = [t._replace(rating=1 if t.ref < 4 else 0) for t in library()]
        game = Game(tracks, rounds=8, rng=random.Random(5))
        self.assertEqual(game.skipped, 4)
        asked = set()
        while game.next_question():
            asked.add(game.question.track.ref)
            self.assertNotIn(game.question.answer, {"Song 0", "Song 1", "Song 2", "Song 3"} if game.question.kind == "title" else set())
            game.answer(None, 15)
        self.assertEqual(asked, set(range(4, 12)))

    def test_error_mentions_skipped_songs(self):
        tracks = [t._replace(rating=1) for t in library()]
        with self.assertRaisesRegex(ValueError, "12 low-rated"):
            Game(tracks)

    def test_rounds_capped_by_library_size(self):
        self.assertEqual(Game(library(6), rounds=10).rounds, 6)


class LyricsTests(unittest.TestCase):
    LRC = "[ar:Someone]\n[offset:+500]\n[00:12.00]First line\n[00:05.50][01:00.00]Chorus\n[00:20.25]\n"

    def test_parse_lrc_sorts_repeats_and_applies_offset(self):
        lyrics = np_lyrics.parse_lrc(self.LRC)
        self.assertTrue(lyrics.synced)
        self.assertEqual(lyrics.lines, ["Chorus", "First line", "", "Chorus"])
        self.assertEqual(lyrics.times, [5.0, 11.5, 19.75, 59.5])

    def test_line_at(self):
        lyrics = np_lyrics.parse_lrc(self.LRC)
        self.assertEqual(lyrics.line_at(0), -1)
        self.assertEqual(lyrics.line_at(5.0), 0)
        self.assertEqual(lyrics.line_at(15), 1)
        self.assertEqual(lyrics.line_at(500), 3)

    def test_plain_text_is_not_synced(self):
        lyrics = np_lyrics.parse_any("Hello\nworld\n", "x")
        self.assertFalse(lyrics.synced)
        self.assertEqual(lyrics.lines, ["Hello", "world"])
        self.assertEqual(lyrics.line_at(10), -1)
        self.assertIsNone(np_lyrics.parse_any("  \n"))

    def test_clean_title(self):
        self.assertEqual(np_lyrics.clean_title("Help! (Remastered 2009)"), "Help!")
        self.assertEqual(np_lyrics.clean_title("Song - Live at Wembley"), "Song")
        self.assertEqual(np_lyrics.clean_title("Track feat. Somebody"), "Track")
        self.assertEqual(np_lyrics.clean_title("Plain"), "Plain")

    def test_sidecar_path(self):
        self.assertEqual(np_lyrics.sidecar_path("file:///music/My%20Song.mp3"), "/music/My Song.lrc")
        self.assertIsNone(np_lyrics.sidecar_path("http://radio/stream"))

    def test_find_prefers_sidecar_then_cache_then_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            song = os.path.join(tmp, "song.ogg")
            cache = os.path.join(tmp, "cache")
            with mock.patch.object(np_lyrics, "fetch_lrclib", return_value="[00:01.00]Net") as fetch:
                found = np_lyrics.find_lyrics("A", "T", "", 100, "file://" + song, cache)
                self.assertEqual((found.lines, found.source), (["Net"], "LRCLIB"))
                found = np_lyrics.find_lyrics("A", "T", "", 100, "file://" + song, cache)
                self.assertEqual(found.source, "cache")
                self.assertEqual(fetch.call_count, 1)

                with open(os.path.join(tmp, "song.lrc"), "w") as f:
                    f.write("[00:02.00]Local")
                found = np_lyrics.find_lyrics("A", "T", "", 100, "file://" + song, cache)
                self.assertEqual((found.lines, found.source), (["Local"], "file"))

            with mock.patch.object(np_lyrics, "fetch_lrclib", return_value=None) as fetch:
                self.assertIsNone(np_lyrics.find_lyrics("B", "U", "", 0, None, cache))
                self.assertIsNone(np_lyrics.find_lyrics("B", "U", "", 0, None, cache))
                self.assertEqual(fetch.call_count, 1)

    def test_lrclib_falls_back_to_search(self):
        responses = [None, [{"duration": 300, "syncedLyrics": "[00:01.00]Far"},
                            {"duration": 201, "plainLyrics": "Near plain"},
                            {"duration": 199, "syncedLyrics": "[00:01.00]Near synced"}]]
        with mock.patch.object(np_lyrics, "_get_json", side_effect=responses):
            self.assertEqual(np_lyrics.fetch_lrclib("A", "T", duration=200), "[00:01.00]Near synced")


if __name__ == "__main__":
    unittest.main()
