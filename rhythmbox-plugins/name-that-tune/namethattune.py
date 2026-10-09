# namethattune.py - a music quiz built from your Rhythmbox library
#
# Plays a short snippet of a random song and asks you to pick its title or
# artist from four choices. Faster answers score more.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
import time

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("RB", "3.0")
from gi.repository import GObject, GLib, Gio, Gtk, Gdk, GdkPixbuf, Peas, RB

import gettext
gettext.install("rhythmbox", RB.locale_dir())

import ntt_game
from ntt_game import Game, Track, SNIPPET_SECONDS

ACTION = "name-that-tune"
# Plugins that would announce the song and spoil the answer.
SPOILER_PLUGINS = ("notification",)
TICK_MS = 100
SKIP_OPTIONS = (
    (0, "Don't skip any songs"),
    (1, "Skip songs rated ★"),
    (2, "Skip songs rated ★★ or less"),
    (3, "Skip songs rated ★★★ or less"),
)

CSS = b"""
.ntt-title { font-size: 22px; font-weight: bold; }
.ntt-big { font-size: 36px; font-weight: bold; }
.ntt-dim { opacity: 0.7; }
.ntt-choice { padding: 14px 10px; font-size: 15px; }
.ntt-star { font-size: 22px; padding: 0 2px; min-width: 0; color: #f5a623; }
.ntt-note { font-size: 12px; opacity: 0.7; }
.ntt-correct, .ntt-correct:disabled {
    background-image: none; background-color: #2e7d32; color: #ffffff;
}
.ntt-wrong, .ntt-wrong:disabled {
    background-image: none; background-color: #c62828; color: #ffffff;
}
"""


def stars(rating):
    rating = int(round(rating or 0))
    return "★" * rating + "☆" * (5 - rating) if rating else _("not rated")


def settings_path():
    return os.path.join(RB.user_data_dir(), "name-that-tune.json")


def load_settings():
    try:
        with open(settings_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings):
    try:
        os.makedirs(os.path.dirname(settings_path()), exist_ok=True)
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(settings, f)
    except OSError:
        pass


FIELD_LABELS = {"title": "Title", "artist": "Artist", "album": "Album", "genre": "Genre"}
MATCH_LABELS = {"is": "is", "contains": "contains"}


def describe_rule(rule):
    return "%s %s \u201c%s\u201d" % (_(FIELD_LABELS.get(rule["field"], rule["field"])),
                                 _(MATCH_LABELS.get(rule["match"], rule["match"])), rule["text"])


class DniDialog(Gtk.Dialog):
    """Edit the Do Not Include list. Changes are saved as you make them."""

    def __init__(self, parent, rules, on_change):
        Gtk.Dialog.__init__(self, title=_("Do Not Include"), transient_for=parent, modal=True)
        self.rules = rules
        self.on_change = on_change
        self.set_default_size(440, 420)
        self.add_button(_("Done"), Gtk.ResponseType.CLOSE)

        box = self.get_content_area()
        box.set_spacing(10)
        box.set_border_width(14)
        intro = Gtk.Label(label=_("Songs that match any rule are left out of the game."),
                          wrap=True, xalign=0)
        box.pack_start(intro, False, False, 0)

        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        empty = Gtk.Label(label=_("Nothing excluded yet."))
        empty.show()
        self.listbox.set_placeholder(empty)
        scroll.add(self.listbox)
        box.pack_start(scroll, True, True, 0)

        add = Gtk.Box(spacing=6)
        self.field_combo = Gtk.ComboBoxText()
        for field in ntt_game.DNI_FIELDS:
            self.field_combo.append(field, _(FIELD_LABELS[field]))
        self.field_combo.set_active_id("artist")
        self.match_combo = Gtk.ComboBoxText()
        for match in ("is", "contains"):
            self.match_combo.append(match, _(MATCH_LABELS[match]))
        self.match_combo.set_active_id("is")
        self.text_entry = Gtk.Entry(placeholder_text=_("e.g. Nickelback"), hexpand=True)
        self.text_entry.connect("activate", lambda e: self._add())
        add_button = Gtk.Button(label=_("Add"))
        add_button.connect("clicked", lambda b: self._add())
        for widget in (self.field_combo, self.match_combo, self.text_entry, add_button):
            add.pack_start(widget, widget is self.text_entry, True, 0)
        box.pack_start(add, False, False, 0)

        self.connect("response", lambda d, r: d.destroy())
        self._refresh()
        self.show_all()

    def _refresh(self):
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        for rule in self.rules:
            row = Gtk.Box(spacing=6, border_width=6)
            row.pack_start(Gtk.Label(label=describe_rule(rule), xalign=0), True, True, 0)
            remove = Gtk.Button.new_from_icon_name("list-remove-symbolic", Gtk.IconSize.BUTTON)
            remove.set_tooltip_text(_("Remove"))
            remove.connect("clicked", lambda b, r=rule: self._remove(r))
            row.pack_start(remove, False, False, 0)
            self.listbox.add(row)
        self.listbox.show_all()

    def _add(self):
        text = self.text_entry.get_text().strip()
        if not text:
            return
        rule = {"field": self.field_combo.get_active_id(),
                "match": self.match_combo.get_active_id(), "text": text}
        if rule not in self.rules:
            self.rules.append(rule)
            self.on_change()
        self.text_entry.set_text("")
        self._refresh()

    def _remove(self, rule):
        self.rules.remove(rule)
        self.on_change()
        self._refresh()


def entry_location(entry):
    return entry.get_string(RB.RhythmDBPropType.LOCATION) if entry else None


class QuizWindow(Gtk.Window):
    def __init__(self, shell):
        Gtk.Window.__init__(self, title=_("Name That Tune"))
        self.shell = shell
        self.player = shell.props.shell_player
        self.db = shell.props.db
        self.settings = load_settings()
        self.dni = self.settings.get("dni", [dict(r) for r in ntt_game.DEFAULT_DNI])
        self.rated_entry = None
        self.game = None
        self.timer_id = 0
        self.seek_id = 0
        self.started_at = None
        self.saved = None
        self.unloaded_plugins = []
        self.art_store = RB.ExtDB(name="album-art")

        self.set_default_size(480, 700)
        self.set_border_width(18)
        self.set_transient_for(shell.props.window)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_start(), "start")
        self.stack.add_named(self._build_question(), "question")
        self.stack.add_named(self._build_results(), "results")
        self.add(self.stack)

        self.connect("destroy", self._on_destroy)
        self.show_all()
        self.stack.set_visible_child_name("start")

    # -- pages -------------------------------------------------------------

    def _label(self, text="", css=None, **kw):
        label = Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER, **kw)
        if css:
            for c in css.split():
                label.get_style_context().add_class(c)
        return label

    def _build_start(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                      valign=Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
        icon.set_pixel_size(96)
        box.pack_start(icon, False, False, 0)
        box.pack_start(self._label(_("Name That Tune"), "ntt-title"), False, False, 0)
        box.pack_start(self._label(
            _("You'll hear %d seconds of a random song from your library. "
              "Pick the right title or artist. The faster you answer, the "
              "more points you get.") % SNIPPET_SECONDS, "ntt-dim"),
            False, False, 0)

        row = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        row.pack_start(Gtk.Label(label=_("Rounds")), False, False, 0)
        self.rounds_spin = Gtk.SpinButton.new_with_range(3, 30, 1)
        self.rounds_spin.set_value(ntt_game.ROUNDS)
        row.pack_start(self.rounds_spin, False, False, 0)
        box.pack_start(row, False, False, 0)

        self.skip_combo = Gtk.ComboBoxText(halign=Gtk.Align.CENTER)
        for value, label in SKIP_OPTIONS:
            self.skip_combo.append(str(value), _(label))
        self.skip_combo.set_active_id(str(self.settings.get("skip_rating", ntt_game.SKIP_RATING)))
        if self.skip_combo.get_active_id() is None:
            self.skip_combo.set_active_id(str(ntt_game.SKIP_RATING))
        self.skip_combo.connect("changed", self._on_skip_changed)
        box.pack_start(self.skip_combo, False, False, 0)
        box.pack_start(self._label(
            _("Rate songs after each round to keep the ones you don't like out of future games."),
            "ntt-note"), False, False, 0)

        self.dni_button = Gtk.Button(halign=Gtk.Align.CENTER)
        self.dni_button.connect("clicked", lambda b: DniDialog(self, self.dni, self._dni_changed))
        box.pack_start(self.dni_button, False, False, 0)
        self._update_dni_button()

        self.start_error = self._label("", "ntt-dim")
        box.pack_start(self.start_error, False, False, 0)

        start = Gtk.Button(label=_("Start"), halign=Gtk.Align.CENTER)
        start.get_style_context().add_class("suggested-action")
        start.set_size_request(160, -1)
        start.connect("clicked", lambda b: self.start_game())
        box.pack_start(start, False, False, 0)
        return box

    def _build_question(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        top = Gtk.Box(spacing=6)
        self.round_label = Gtk.Label(xalign=0)
        self.score_label = Gtk.Label(xalign=1)
        top.pack_start(self.round_label, True, True, 0)
        top.pack_start(self.score_label, True, True, 0)
        box.pack_start(top, False, False, 0)

        self.progress = Gtk.ProgressBar()
        box.pack_start(self.progress, False, False, 0)

        self.art = Gtk.Image()
        self.art.set_size_request(160, 160)
        box.pack_start(self.art, False, False, 6)

        self.prompt_label = self._label("", "ntt-title")
        box.pack_start(self.prompt_label, False, False, 0)

        grid = Gtk.Grid(row_spacing=8, column_spacing=8,
                        row_homogeneous=True, column_homogeneous=True)
        self.choice_buttons = []
        for i in range(ntt_game.CHOICES):
            button = Gtk.Button(hexpand=True)
            button.get_style_context().add_class("ntt-choice")
            label = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER,
                              max_width_chars=22)
            button.add(label)
            button.connect("clicked", self._on_choice)
            grid.attach(button, i % 2, i // 2, 1, 1)
            self.choice_buttons.append(button)
        box.pack_start(grid, False, False, 0)

        self.reveal_label = self._label("")
        box.pack_start(self.reveal_label, False, False, 0)

        self.rating_box = Gtk.Box(spacing=0, halign=Gtk.Align.CENTER)
        self.star_buttons = []
        for n in range(1, 6):
            button = Gtk.Button(label="☆", relief=Gtk.ReliefStyle.NONE)
            button.get_style_context().add_class("ntt-star")
            button.set_tooltip_text(gettext.ngettext("%d star", "%d stars", n) % n)
            button.connect("clicked", lambda b, n=n: self._set_rating(n))
            self.rating_box.pack_start(button, False, False, 0)
            self.star_buttons.append(button)
        never = Gtk.Button(label=_("👎 Never again"))
        never.set_tooltip_text(_("Rate 1 star so it's left out of future games"))
        never.connect("clicked", lambda b: self._set_rating(1))
        self.rating_box.pack_start(never, False, False, 12)
        box.pack_start(self.rating_box, False, False, 0)
        self.exclude_artist_button = Gtk.Button(halign=Gtk.Align.CENTER)
        self.exclude_artist_button.connect("clicked", lambda b: self._exclude_current_artist())
        box.pack_start(self.exclude_artist_button, False, False, 0)
        self.rating_note = self._label("", "ntt-note")
        box.pack_start(self.rating_note, False, False, 0)

        actions = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.replay_button = Gtk.Button(label=_("Replay snippet"))
        self.replay_button.connect("clicked", lambda b: self._seek_to_start())
        self.next_button = Gtk.Button(label=_("Next"))
        self.next_button.get_style_context().add_class("suggested-action")
        self.next_button.connect("clicked", lambda b: self.next_round())
        actions.pack_start(self.replay_button, False, False, 0)
        actions.pack_start(self.next_button, False, False, 0)
        box.pack_end(actions, False, False, 0)
        return box

    def _build_results(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      valign=Gtk.Align.CENTER)
        box.pack_start(self._label(_("Final score"), "ntt-dim"), False, False, 0)
        self.final_score = self._label("", "ntt-big")
        box.pack_start(self.final_score, False, False, 0)
        self.final_detail = self._label("")
        box.pack_start(self.final_detail, False, False, 0)
        self.final_list = self._label("", "ntt-dim", xalign=0, halign=Gtk.Align.CENTER)
        self.final_list.set_justify(Gtk.Justification.LEFT)
        box.pack_start(self.final_list, False, False, 0)

        row = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        again = Gtk.Button(label=_("Play again"))
        again.get_style_context().add_class("suggested-action")
        again.connect("clicked", lambda b: self.start_game())
        close = Gtk.Button(label=_("Close"))
        close.connect("clicked", lambda b: self.destroy())
        row.pack_start(again, False, False, 0)
        row.pack_start(close, False, False, 0)
        box.pack_start(row, False, False, 0)
        return box

    # -- game flow ---------------------------------------------------------

    def _library_tracks(self):
        model = self.shell.props.library_source.props.base_query_model
        tracks = []
        for row in model:
            entry = row[0]
            tracks.append(Track(
                entry.get_string(RB.RhythmDBPropType.TITLE),
                entry.get_string(RB.RhythmDBPropType.ARTIST),
                entry.get_string(RB.RhythmDBPropType.ALBUM),
                entry.get_ulong(RB.RhythmDBPropType.DURATION),
                entry,
                entry.get_double(RB.RhythmDBPropType.RATING),
                entry.get_string(RB.RhythmDBPropType.GENRE)))
        return tracks

    def start_game(self):
        try:
            self.game = Game(self._library_tracks(),
                             rounds=int(self.rounds_spin.get_value()),
                             unknown=_("Unknown"),
                             skip_rating=self._skip_rating(),
                             dni=self.dni)
        except ValueError as e:
            self.start_error.set_text(str(e))
            self.stack.set_visible_child_name("start")
            return
        self.start_error.set_text("")
        self._hide_spoilers()
        self.next_round()

    def next_round(self):
        self._stop_timer()
        q = self.game.next_question()
        if q is None:
            self._show_results()
            return

        self.round_label.set_text(_("Round %d of %d") % (self.game.round, self.game.rounds))
        self._update_score()
        self.prompt_label.set_text(
            _("Who's the artist?") if q.kind == "artist" else _("What's this song called?"))
        for button, choice in zip(self.choice_buttons, q.choices):
            button.get_child().set_text(choice)
            button.set_sensitive(True)
            ctx = button.get_style_context()
            ctx.remove_class("ntt-correct")
            ctx.remove_class("ntt-wrong")
        self.art.set_from_icon_name("dialog-question", Gtk.IconSize.DIALOG)
        self.art.set_pixel_size(128)
        self.reveal_label.set_text("")
        self.rating_box.set_visible(False)
        self.exclude_artist_button.set_visible(False)
        self.rating_note.set_text("")
        self.rated_entry = None
        self.next_button.set_visible(False)
        self.replay_button.set_visible(True)
        self.progress.set_fraction(1.0)
        self.stack.set_visible_child_name("question")

        self.started_at = None
        source = self.shell.props.library_source
        self.player.play_entry(q.track.ref, source)
        self._seek_to_start()

    def _seek_to_start(self):
        """Seek once the new song has actually started, then start the clock."""
        if self.seek_id:
            GLib.source_remove(self.seek_id)
        q = self.game.question
        if q is None:
            return
        tries = [0]

        def attempt():
            tries[0] += 1
            playing = self.player.get_playing_entry()
            if entry_location(playing) == entry_location(q.track.ref):
                try:
                    self.player.set_playing_time(q.start)
                    ok, pos = self.player.get_playing_time()
                except GLib.Error:
                    ok, pos = False, 0
                if ok and abs(pos - q.start) <= 2:
                    try:
                        self.player.play()
                    except GLib.Error:
                        pass
                    if self.started_at is None:
                        self.started_at = time.monotonic()
                        self.timer_id = GLib.timeout_add(TICK_MS, self._tick)
                    self.seek_id = 0
                    return False
            if tries[0] > 40:
                # Couldn't seek (e.g. a stream); just play from wherever it is.
                if self.started_at is None:
                    self.started_at = time.monotonic()
                    self.timer_id = GLib.timeout_add(TICK_MS, self._tick)
                self.seek_id = 0
                return False
            return True

        self.seek_id = GLib.timeout_add(150, attempt)

    def _elapsed(self):
        return 0 if self.started_at is None else time.monotonic() - self.started_at

    def _tick(self):
        elapsed = self._elapsed()
        self.progress.set_fraction(max(0.0, 1 - elapsed / SNIPPET_SECONDS))
        if elapsed >= SNIPPET_SECONDS:
            self.timer_id = 0
            self._pause()
            self._finish_round(None)
            return False
        return True

    def _on_choice(self, button):
        self._finish_round(button.get_child().get_text())

    def _finish_round(self, choice):
        if self.game is None or self.game.question is None:
            return
        self._stop_timer()
        q = self.game.question
        correct, points = self.game.answer(choice, self._elapsed())

        for button in self.choice_buttons:
            text = button.get_child().get_text()
            button.set_sensitive(False)
            if text == q.answer:
                button.get_style_context().add_class("ntt-correct")
            elif text == choice:
                button.get_style_context().add_class("ntt-wrong")

        if choice is None:
            verdict = _("Time's up!")
        elif correct:
            verdict = _("Correct! +%d") % points
            if self.game.streak > 1:
                verdict += "  " + _("🔥 %d in a row") % self.game.streak
        else:
            verdict = _("Not quite.")
        t = q.track
        self.reveal_label.set_markup("<b>%s</b>\n%s — %s\n<small>%s</small>" % tuple(
            GLib.markup_escape_text(s) for s in (verdict, t.title, t.artist, t.album)))
        self._update_score()
        self._load_art(t.ref)
        self.rated_entry = t.ref
        self._show_rating()
        self.rating_box.set_visible(True)
        self.exclude_artist_button.set_label(_("🚫 Don't include %s") % t.artist)
        self.exclude_artist_button.set_sensitive(True)
        self.exclude_artist_button.set_visible(True)

        self.replay_button.set_visible(False)
        self.next_button.set_label(
            _("See results") if self.game.round >= self.game.rounds else _("Next"))
        self.next_button.set_visible(True)
        self.next_button.grab_focus()

    def _show_results(self):
        self._pause()
        g = self.game
        self.final_score.set_text("%d" % g.score)
        self.final_detail.set_text(
            _("%d of %d correct · best streak %d") % (g.correct, g.rounds, g.best_streak))
        lines = []
        for q, choice, correct, points in g.history:
            mark = "✓" if correct else "✗"
            rating = q.track.ref.get_double(RB.RhythmDBPropType.RATING)
            lines.append("%s  %s — %s   %s" % (mark, q.track.title, q.track.artist, stars(rating)))
        self.final_list.set_text("\n".join(lines))
        self.stack.set_visible_child_name("results")
        self._restore_player()

    def _skip_rating(self):
        return int(self.skip_combo.get_active_id() or 0)

    def _on_skip_changed(self, combo):
        self.settings["skip_rating"] = self._skip_rating()
        save_settings(self.settings)

    def _dni_changed(self):
        self.settings["dni"] = self.dni
        save_settings(self.settings)
        self._update_dni_button()

    def _update_dni_button(self):
        self.dni_button.set_label(_("Do Not Include list (%d)…") % len(self.dni))

    def _exclude_current_artist(self):
        if self.rated_entry is None:
            return
        artist = self.rated_entry.get_string(RB.RhythmDBPropType.ARTIST)
        rule = {"field": "artist", "match": "is", "text": artist}
        if rule not in self.dni:
            self.dni.append(rule)
            self._dni_changed()
        if self.game is not None:
            self.game.exclude_upcoming(rule)
            self.next_button.set_label(
                _("See results") if self.game.round >= self.game.rounds else _("Next"))
        self.exclude_artist_button.set_label(_("%s won't come up again") % artist)
        self.exclude_artist_button.set_sensitive(False)

    def _set_rating(self, rating):
        """Save a star rating to the Rhythmbox library."""
        entry = self.rated_entry
        if entry is None:
            return
        self.db.entry_set(entry, RB.RhythmDBPropType.RATING, float(rating))
        self.db.commit()
        self._show_rating()

    def _show_rating(self):
        rating = int(round(self.rated_entry.get_double(RB.RhythmDBPropType.RATING)))
        for n, button in enumerate(self.star_buttons, 1):
            button.set_label("★" if n <= rating else "☆")
        if ntt_game.is_skipped(rating, self._skip_rating()):
            self.rating_note.set_text(_("Got it. This song won't come up in future games."))
        elif rating:
            self.rating_note.set_text(_("Saved to your Rhythmbox library."))
        else:
            self.rating_note.set_text(_("Rate this song?"))

    def _update_score(self):
        self.score_label.set_markup(_("Score <b>%d</b>") % self.game.score)

    def _load_art(self, entry):
        key = entry.create_ext_db_key(RB.RhythmDBPropType.ALBUM)
        expected = self.game.history[-1][0] if self.game.history else None

        def done(key, store_key, filename, data):
            # Ignore late results from an earlier round.
            if not self.game or not self.game.history or self.game.history[-1][0] is not expected:
                return
            if data is not None and hasattr(data, "scale_simple"):
                self.art.set_from_pixbuf(data.scale_simple(160, 160, GdkPixbuf.InterpType.BILINEAR))
            else:
                self.art.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
                self.art.set_pixel_size(128)

        self.art_store.request(key, done)

    # -- player helpers ----------------------------------------------------

    def _pause(self):
        try:
            self.player.pause()
        except GLib.Error:
            pass

    def _stop_timer(self):
        for attr in ("timer_id", "seek_id"):
            source_id = getattr(self, attr)
            if source_id:
                GLib.source_remove(source_id)
                setattr(self, attr, 0)

    def _hide_spoilers(self):
        """Save what was playing, hide the main window and mute song popups."""
        if self.saved is None:
            entry = self.player.get_playing_entry()
            try:
                ok, pos = self.player.get_playing_time()
                ok2, playing = self.player.get_playing()
            except GLib.Error:
                pos, playing = 0, False
            self.saved = (entry, self.player.get_playing_source(), pos, playing)

        self.shell.props.window.iconify()

        engine = Peas.Engine.get_default()
        for name in SPOILER_PLUGINS:
            info = engine.get_plugin_info(name)
            if info is not None and info.is_loaded():
                engine.unload_plugin(info)
                self.unloaded_plugins.append(info)

    def _restore_player(self):
        engine = Peas.Engine.get_default()
        for info in self.unloaded_plugins:
            engine.load_plugin(info)
        self.unloaded_plugins = []

        if self.saved is None:
            return
        entry, source, pos, playing = self.saved
        self.saved = None
        if entry is None:
            self._pause()
            return
        self.player.play_entry(entry, source or self.shell.props.library_source)
        tries = [0]

        def resume():
            tries[0] += 1
            if entry_location(self.player.get_playing_entry()) == entry_location(entry):
                try:
                    self.player.set_playing_time(pos)
                except GLib.Error:
                    pass
                if not playing:
                    self._pause()
                return False
            return tries[0] < 40

        GLib.timeout_add(150, resume)

    def _on_destroy(self, window):
        self._stop_timer()
        if self.saved is not None:
            self._pause()
            self._restore_player()
        self.shell.props.window.present()


class NameThatTunePlugin(GObject.Object, Peas.Activatable):
    __gtype_name__ = "NameThatTunePlugin"
    object = GObject.Property(type=GObject.Object)

    def __init__(self):
        GObject.Object.__init__(self)
        self.window = None

    def do_activate(self):
        shell = self.object
        self.action = Gio.SimpleAction.new(ACTION, None)
        self.action.connect("activate", self.open_quiz)
        shell.props.window.add_action(self.action)

        item = Gio.MenuItem.new(label=_("Name That Tune…"), detailed_action="win." + ACTION)
        shell.props.application.add_plugin_menu_item("tools", ACTION, item)

    def do_deactivate(self):
        shell = self.object
        shell.props.application.remove_plugin_menu_item("tools", ACTION)
        shell.props.window.remove_action(ACTION)
        self.action = None
        if self.window is not None:
            self.window.destroy()
            self.window = None

    def open_quiz(self, action, parameter):
        if self.window is not None:
            self.window.present()
            return
        self.window = QuizWindow(self.object)
        self.window.connect("destroy", self._window_closed)

    def _window_closed(self, window):
        self.window = None
