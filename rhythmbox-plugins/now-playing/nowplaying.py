# nowplaying.py - a big "Now Playing" display for Rhythmbox
#
# Album art with a background tinted to match it, track details, seek bar,
# transport and volume controls, time-synced lyrics you can click to seek,
# and an Up Next view of the play queue you can reorder.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import threading

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("RB", "3.0")
from gi.repository import GObject, GLib, Gio, Gtk, Gdk, GdkPixbuf, Pango, Peas, RB

import gettext
gettext.install("rhythmbox", RB.locale_dir())

import np_lyrics

ACTION = "now-playing-display"
ART_SIZE = 360
TICK_MS = 250
FALLBACK_RGB = (60, 60, 72)

BASE_CSS = b"""
#np-window label { color: #ffffff; }
#np-window .np-title { font-size: 26px; font-weight: bold; }
#np-window .np-artist { font-size: 18px; }
#np-window .np-dim { color: rgba(255,255,255,0.65); }
#np-window .np-lyric { font-size: 17px; color: rgba(255,255,255,0.45); padding: 4px 8px; }
#np-window .np-lyric-current { font-size: 21px; font-weight: bold; color: #ffffff; }
#np-window .np-lyric-plain { color: rgba(255,255,255,0.9); }
#np-window list, #np-window list row, #np-window scrolledwindow, #np-window viewport {
    background: transparent; border: none;
}
#np-window list row:hover { background-color: rgba(255,255,255,0.08); }
#np-window button.np-flat {
    background: transparent; border: none; box-shadow: none; color: #ffffff;
    -gtk-icon-shadow: none;
}
#np-window button.np-flat:hover { background-color: rgba(255,255,255,0.12); }
#np-window button.np-play { background-color: rgba(255,255,255,0.18); border-radius: 999px; }
#np-window stackswitcher button { color: #ffffff; background: transparent; }
#np-window stackswitcher button:checked { background-color: rgba(255,255,255,0.18); }
#np-window scale trough { background-color: rgba(255,255,255,0.2); }
#np-window scale highlight { background-color: #ffffff; }
"""


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)
    return "%d:%02d" % (seconds // 60, seconds % 60)


def average_rgb(pixbuf):
    """Average colour of a pixbuf, darkened enough for white text."""
    small = pixbuf.scale_simple(1, 1, GdkPixbuf.InterpType.BILINEAR)
    r, g, b = small.get_pixels()[:3]
    # Keep the hue but cap brightness so the white text stays readable.
    peak = max(r, g, b, 1)
    scale = min(1.0, 110 / peak)
    return int(r * scale), int(g * scale), int(b * scale)


def flat_button(icon, tooltip, size=Gtk.IconSize.LARGE_TOOLBAR):
    button = Gtk.Button.new_from_icon_name(icon, size)
    button.set_tooltip_text(tooltip)
    button.set_relief(Gtk.ReliefStyle.NONE)
    button.get_style_context().add_class("np-flat")
    return button


class NowPlayingWindow(Gtk.Window):
    def __init__(self, shell):
        Gtk.Window.__init__(self, title=_("Now Playing"))
        self.set_name("np-window")
        self.shell = shell
        self.player = shell.props.shell_player
        self.db = shell.props.db
        self.entry = None
        self.lyrics = None
        self.lyric_rows = []
        self.current_line = -2
        self.lyrics_generation = 0
        self.seeking = False
        self.fullscreen_on = False
        self.queue_model = None
        self.queue_handlers = []
        self.queue_refresh_id = 0
        self.handlers = []
        self.cache_dir = os.path.join(RB.user_cache_dir(), "now-playing-lyrics")
        self.art_store = RB.ExtDB(name="album-art")

        self.set_default_size(1000, 620)
        self.set_transient_for(None)

        base = Gtk.CssProvider()
        base.load_from_data(BASE_CSS)
        self.tint = Gtk.CssProvider()
        screen = Gdk.Screen.get_default()
        Gtk.StyleContext.add_provider_for_screen(screen, base, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.StyleContext.add_provider_for_screen(screen, self.tint, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
        self._set_tint(FALLBACK_RGB)

        root = Gtk.Box(spacing=32, border_width=28)
        root.pack_start(self._build_left(), False, False, 0)
        root.pack_start(self._build_right(), True, True, 0)
        self.add(root)

        self._connect(self.player, "playing-song-changed", self._on_song_changed)
        self._connect(self.player, "playing-changed", lambda p, playing: self._update_play_button())
        self._connect(self.player, "notify::volume", lambda *a: self._sync_volume())
        self._connect(self.art_store, "added", self._on_art_added)
        self._connect(shell.props.queue_source, "notify::query-model", lambda *a: self._watch_queue())
        self.connect("key-press-event", self._on_key)
        self.connect("destroy", self._on_destroy)

        self._watch_queue()
        self._on_song_changed(self.player, self.player.get_playing_entry())
        self.tick_id = GLib.timeout_add(TICK_MS, self._tick)
        self.show_all()

    def _connect(self, obj, signal, handler):
        self.handlers.append((obj, obj.connect(signal, handler)))

    # -- layout ------------------------------------------------------------

    def _build_left(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_size_request(ART_SIZE, -1)

        self.art = Gtk.Image()
        self.art.set_size_request(ART_SIZE, ART_SIZE)
        box.pack_start(self.art, False, False, 0)

        self.title_label = Gtk.Label(xalign=0, wrap=True, max_width_chars=28)
        self.title_label.get_style_context().add_class("np-title")
        self.artist_label = Gtk.Label(xalign=0, wrap=True, max_width_chars=32)
        self.artist_label.get_style_context().add_class("np-artist")
        self.album_label = Gtk.Label(xalign=0, wrap=True, max_width_chars=36)
        self.album_label.get_style_context().add_class("np-dim")
        for label in (self.title_label, self.artist_label, self.album_label):
            box.pack_start(label, False, False, 0)

        self.seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.seek.set_draw_value(False)
        self.seek.connect("button-press-event", self._seek_start)
        self.seek.connect("button-release-event", self._seek_end)
        box.pack_start(self.seek, False, False, 4)

        times = Gtk.Box()
        self.elapsed_label = Gtk.Label(xalign=0)
        self.duration_label = Gtk.Label(xalign=1)
        for label in (self.elapsed_label, self.duration_label):
            label.get_style_context().add_class("np-dim")
        times.pack_start(self.elapsed_label, True, True, 0)
        times.pack_start(self.duration_label, True, True, 0)
        box.pack_start(times, False, False, 0)

        controls = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        prev = flat_button("media-skip-backward-symbolic", _("Previous"))
        prev.connect("clicked", lambda b: self._call(self.player.do_previous))
        self.play_button = flat_button("media-playback-start-symbolic", _("Play"), Gtk.IconSize.DIALOG)
        self.play_button.get_style_context().add_class("np-play")
        self.play_button.connect("clicked", lambda b: self._call(self.player.playpause))
        nxt = flat_button("media-skip-forward-symbolic", _("Next"))
        nxt.connect("clicked", lambda b: self._call(self.player.do_next))
        for b in (prev, self.play_button, nxt):
            controls.pack_start(b, False, False, 0)
        box.pack_start(controls, False, False, 6)

        bottom = Gtk.Box(spacing=6)
        self.volume = Gtk.VolumeButton()
        self.volume.get_style_context().add_class("np-flat")
        self._sync_volume()
        self.volume.connect("value-changed", self._on_volume)
        self.fs_button = flat_button("view-fullscreen-symbolic", _("Full screen (F11)"), Gtk.IconSize.BUTTON)
        self.fs_button.connect("clicked", lambda b: self.toggle_fullscreen())
        bottom.pack_start(self.volume, False, False, 0)
        bottom.pack_end(self.fs_button, False, False, 0)
        box.pack_end(bottom, False, False, 0)
        return box

    def _build_right(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        switcher = Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.CENTER)
        box.pack_start(switcher, False, False, 0)

        # Lyrics
        lyrics_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.lyrics_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.lyrics_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.lyrics_list.connect("row-activated", self._on_lyric_clicked)
        self.lyrics_scroll.add(self.lyrics_list)
        lyrics_box.pack_start(self.lyrics_scroll, True, True, 0)
        self.lyrics_status = Gtk.Label(xalign=1)
        self.lyrics_status.get_style_context().add_class("np-dim")
        lyrics_box.pack_start(self.lyrics_status, False, False, 0)
        self.stack.add_titled(lyrics_box, "lyrics", _("Lyrics"))

        # Up next
        queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header = Gtk.Box(spacing=6)
        self.queue_summary = Gtk.Label(xalign=0)
        self.queue_summary.get_style_context().add_class("np-dim")
        clear = Gtk.Button(label=_("Clear"))
        clear.get_style_context().add_class("np-flat")
        clear.connect("clicked", lambda b: self._clear_queue())
        header.pack_start(self.queue_summary, True, True, 0)
        header.pack_end(clear, False, False, 0)
        queue_box.pack_start(header, False, False, 0)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.queue_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.queue_list.connect("row-activated", self._on_queue_row_activated)
        placeholder = Gtk.Label(
            label=_("The play queue is empty.\nRight-click songs in Rhythmbox and choose "
                    "“Add to Play Queue”."),
            justify=Gtk.Justification.CENTER)
        placeholder.get_style_context().add_class("np-dim")
        placeholder.show()
        self.queue_list.set_placeholder(placeholder)
        scroll.add(self.queue_list)
        queue_box.pack_start(scroll, True, True, 0)
        self.stack.add_titled(queue_box, "queue", _("Up Next"))

        box.pack_start(self.stack, True, True, 0)
        return box

    # -- current song ------------------------------------------------------

    def _on_song_changed(self, player, entry):
        self.entry = entry
        self._queue_changed()
        self.lyrics_generation += 1
        if entry is None:
            self.title_label.set_text(_("Nothing playing"))
            self.artist_label.set_text("")
            self.album_label.set_text("")
            self.duration_label.set_text("")
            self.seek.set_range(0, 1)
            self._show_art(None)
            self._set_lyrics(None, "")
            self._update_play_button()
            return

        get = entry.get_string
        self.title_label.set_text(get(RB.RhythmDBPropType.TITLE))
        self.artist_label.set_text(get(RB.RhythmDBPropType.ARTIST))
        album = get(RB.RhythmDBPropType.ALBUM)
        year = entry.get_ulong(RB.RhythmDBPropType.YEAR)
        self.album_label.set_text("%s · %d" % (album, year) if year else album)
        duration = entry.get_ulong(RB.RhythmDBPropType.DURATION)
        self.seek.set_range(0, max(1, duration))
        self.duration_label.set_text(fmt_time(duration) if duration else "")
        self.set_title("%s — %s" % (get(RB.RhythmDBPropType.TITLE), get(RB.RhythmDBPropType.ARTIST)))
        self._update_play_button()

        self._show_art(None)
        self.art_store.request(entry.create_ext_db_key(RB.RhythmDBPropType.ALBUM), self._on_art)
        self._load_lyrics(entry)

    def _on_art(self, key, store_key, filename, data):
        if self.entry is not None and self.db.entry_matches_ext_db_key(self.entry, store_key or key):
            self._show_art(data)

    def _on_art_added(self, store, key, filename, data):
        # Art found later (e.g. by the Cover Art Search plugin).
        if self.entry is not None and self.db.entry_matches_ext_db_key(self.entry, key):
            self._show_art(data)

    def _show_art(self, pixbuf):
        if pixbuf is None or not hasattr(pixbuf, "scale_simple"):
            self.art.set_from_icon_name("audio-x-generic", Gtk.IconSize.DIALOG)
            self.art.set_pixel_size(ART_SIZE // 2)
            self._set_tint(FALLBACK_RGB)
            return
        w, h = pixbuf.get_width(), pixbuf.get_height()
        scale = ART_SIZE / max(w, h)
        self.art.set_from_pixbuf(pixbuf.scale_simple(
            max(1, int(w * scale)), max(1, int(h * scale)), GdkPixbuf.InterpType.BILINEAR))
        self._set_tint(average_rgb(pixbuf))

    def _set_tint(self, rgb):
        r, g, b = rgb
        css = ("#np-window { background-image: linear-gradient(160deg, rgb(%d,%d,%d) 0%%, "
               "rgb(%d,%d,%d) 55%%, #0d0d10 100%%); }" % (r, g, b, r // 2, g // 2, b // 2))
        self.tint.load_from_data(css.encode())

    def _update_play_button(self):
        try:
            ok, playing = self.player.get_playing()
        except GLib.Error:
            playing = False
        icon = "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
        self.play_button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.DIALOG))
        self.play_button.set_tooltip_text(_("Pause") if playing else _("Play"))

    # -- position, seeking, volume -------------------------------------------

    def _position(self):
        """Playback position in seconds, with sub-second precision."""
        try:
            ns = self.player.props.player.get_time()
            if ns >= 0:
                return ns / 1e9
        except Exception:
            pass
        try:
            ok, pos = self.player.get_playing_time()
            return float(pos) if ok else 0.0
        except GLib.Error:
            return 0.0

    def _tick(self):
        if self.entry is None:
            return True
        pos = self._position()
        if not self.seeking:
            self.seek.set_value(pos)
        self.elapsed_label.set_text(fmt_time(pos))
        self._highlight_lyric(pos)
        return True

    def _seek_start(self, widget, event):
        self.seeking = True
        return False

    def _seek_end(self, widget, event):
        self.seeking = False
        self._seek_to(self.seek.get_value())
        return False

    def _seek_to(self, seconds):
        try:
            self.player.set_playing_time(int(seconds))
        except GLib.Error:
            pass

    def _sync_volume(self):
        if hasattr(self, "volume"):
            value = self.player.props.volume
            if abs(self.volume.get_value() - value) > 0.001:
                self.volume.set_value(value)

    def _on_volume(self, button, value):
        if abs(self.player.props.volume - value) > 0.001:
            self.player.props.volume = value

    def _call(self, method):
        try:
            method()
        except GLib.Error:
            pass

    # -- lyrics --------------------------------------------------------------

    def _load_lyrics(self, entry):
        generation = self.lyrics_generation
        get = entry.get_string
        args = (get(RB.RhythmDBPropType.ARTIST), get(RB.RhythmDBPropType.TITLE),
                get(RB.RhythmDBPropType.ALBUM), entry.get_ulong(RB.RhythmDBPropType.DURATION),
                get(RB.RhythmDBPropType.LOCATION), self.cache_dir)
        self._set_lyrics(None, _("Looking for lyrics…"))

        def work():
            try:
                result, error = np_lyrics.find_lyrics(*args), None
            except Exception as e:
                result, error = None, e
            GLib.idle_add(done, result, error)

        def done(result, error):
            if generation != self.lyrics_generation:
                return False
            if error is not None:
                self._set_lyrics(None, _("Couldn't reach the lyrics service."))
            elif result is None:
                self._set_lyrics(None, _("No lyrics found for this song."))
            else:
                self._set_lyrics(result, "")
            return False

        threading.Thread(target=work, daemon=True).start()

    def _set_lyrics(self, lyrics, status):
        self.lyrics = lyrics
        self.current_line = -2
        self.lyric_rows = []
        for child in self.lyrics_list.get_children():
            self.lyrics_list.remove(child)

        if lyrics is None:
            self.lyrics_status.set_text("")
            label = Gtk.Label(label=status, wrap=True)
            label.get_style_context().add_class("np-dim")
            row = Gtk.ListBoxRow(activatable=False)
            row.add(label)
            self.lyrics_list.add(row)
        else:
            for i, line in enumerate(lyrics.lines):
                label = Gtk.Label(label=line or "♪", xalign=0, wrap=True,
                                  wrap_mode=Pango.WrapMode.WORD_CHAR)
                ctx = label.get_style_context()
                ctx.add_class("np-lyric")
                if not lyrics.synced:
                    ctx.add_class("np-lyric-plain")
                row = Gtk.ListBoxRow(activatable=lyrics.synced)
                row.add(label)
                row.line_index = i
                self.lyrics_list.add(row)
                self.lyric_rows.append(row)
            note = _("Synced lyrics · click a line to jump there") if lyrics.synced else ""
            source = _("from %s") % lyrics.source if lyrics.source else ""
            self.lyrics_status.set_text("  ·  ".join(s for s in (note, source) if s))
        self.lyrics_list.show_all()
        self.lyrics_scroll.get_vadjustment().set_value(0)

    def _highlight_lyric(self, pos):
        if self.lyrics is None or not self.lyrics.synced:
            return
        index = self.lyrics.line_at(pos + 0.2)
        if index == self.current_line:
            return
        if 0 <= self.current_line < len(self.lyric_rows):
            self.lyric_rows[self.current_line].get_child().get_style_context().remove_class("np-lyric-current")
        self.current_line = index
        if index < 0:
            return
        row = self.lyric_rows[index]
        row.get_child().get_style_context().add_class("np-lyric-current")
        GLib.idle_add(self._center_row, row)

    def _center_row(self, row):
        alloc = row.get_allocation()
        adj = self.lyrics_scroll.get_vadjustment()
        target = alloc.y + alloc.height / 2 - adj.get_page_size() / 2
        adj.set_value(max(adj.get_lower(), min(target, adj.get_upper() - adj.get_page_size())))
        return False

    def _on_lyric_clicked(self, listbox, row):
        if self.lyrics is not None and self.lyrics.synced:
            self._seek_to(self.lyrics.times[row.line_index])

    # -- play queue ------------------------------------------------------------

    def _queue(self):
        return self.shell.props.queue_source

    def _watch_queue(self):
        for handler in self.queue_handlers:
            self.queue_model.disconnect(handler)
        self.queue_handlers = []
        self.queue_model = self._queue().props.query_model
        if self.queue_model is not None:
            for signal in ("row-inserted", "row-deleted", "rows-reordered"):
                self.queue_handlers.append(self.queue_model.connect(signal, self._queue_changed))
        self._refresh_queue()

    def _queue_changed(self, *args):
        # Batch bursts of changes (adding an album fires one signal per song).
        if not self.queue_refresh_id:
            self.queue_refresh_id = GLib.idle_add(self._refresh_queue)

    def _queue_entries(self):
        return [row[0] for row in self.queue_model] if self.queue_model is not None else []

    def _upcoming(self):
        """(queue index, entry) pairs, minus the song playing from the queue.

        Rhythmbox keeps the current song in the queue until it finishes, but
        it isn't "up next" any more, so leave it out of the list.
        """
        playing = self.player.get_playing_entry()
        skip = None
        if playing is not None and self.player.props.playing_from_queue:
            skip = playing.get_string(RB.RhythmDBPropType.LOCATION)
        return [(i, e) for i, e in enumerate(self._queue_entries())
                if e.get_string(RB.RhythmDBPropType.LOCATION) != skip]

    def _refresh_queue(self):
        self.queue_refresh_id = 0
        for child in self.queue_list.get_children():
            self.queue_list.remove(child)
        upcoming = self._upcoming()
        total = 0
        for n, (index, entry) in enumerate(upcoming):
            duration = entry.get_ulong(RB.RhythmDBPropType.DURATION)
            total += duration
            first, last = n == 0, n == len(upcoming) - 1
            self.queue_list.add(self._queue_row(entry, n + 1, index, first, last, duration))
        if upcoming:
            self.queue_summary.set_text(
                gettext.ngettext("%d song", "%d songs", len(upcoming)) % len(upcoming)
                + " · " + fmt_time(total))
        else:
            self.queue_summary.set_text(_("Play queue"))
        self.queue_list.show_all()
        return False

    def _queue_row(self, entry, number, index, first, last, duration):
        row = Gtk.ListBoxRow()
        row.entry = entry
        box = Gtk.Box(spacing=10, border_width=6)

        position = Gtk.Label(label=str(number), width_chars=3, xalign=1)
        position.get_style_context().add_class("np-dim")
        box.pack_start(position, False, False, 0)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        title.set_markup("<b>%s</b>" % GLib.markup_escape_text(entry.get_string(RB.RhythmDBPropType.TITLE)))
        artist = Gtk.Label(label=entry.get_string(RB.RhythmDBPropType.ARTIST),
                           xalign=0, ellipsize=Pango.EllipsizeMode.END)
        artist.get_style_context().add_class("np-dim")
        text.pack_start(title, False, False, 0)
        text.pack_start(artist, False, False, 0)
        box.pack_start(text, True, True, 0)

        length = Gtk.Label(label=fmt_time(duration) if duration else "")
        length.get_style_context().add_class("np-dim")
        box.pack_start(length, False, False, 0)

        up = flat_button("go-up-symbolic", _("Move up"), Gtk.IconSize.BUTTON)
        up.set_sensitive(not first)
        up.connect("clicked", lambda b: self._move(entry, index - 1))
        down = flat_button("go-down-symbolic", _("Move down"), Gtk.IconSize.BUTTON)
        down.set_sensitive(not last)
        down.connect("clicked", lambda b: self._move(entry, index + 1))
        remove = flat_button("list-remove-symbolic", _("Remove from queue"), Gtk.IconSize.BUTTON)
        remove.connect("clicked", lambda b: self._queue().remove_entry(entry))
        for b in (up, down, remove):
            box.pack_start(b, False, False, 0)

        row.add(box)
        row.set_tooltip_text(_("Double-click to play now"))
        return row

    def _move(self, entry, index):
        self._queue().move_entry(entry, index)

    def _clear_queue(self):
        queue = self._queue()
        for index, entry in self._upcoming():
            queue.remove_entry(entry)

    def _on_queue_row_activated(self, listbox, row):
        self.player.play_entry(row.entry, self._queue())

    # -- window ----------------------------------------------------------------

    def toggle_fullscreen(self):
        if self.fullscreen_on:
            self.unfullscreen()
        else:
            self.fullscreen()
        self.fullscreen_on = not self.fullscreen_on
        icon = "view-restore-symbolic" if self.fullscreen_on else "view-fullscreen-symbolic"
        self.fs_button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.BUTTON))

    def _on_key(self, widget, event):
        key = event.keyval
        if key == Gdk.KEY_F11 or (key == Gdk.KEY_Escape and self.fullscreen_on):
            self.toggle_fullscreen()
        elif key == Gdk.KEY_space and not isinstance(self.get_focus(), Gtk.Entry):
            self._call(self.player.playpause)
        else:
            return False
        return True

    def _on_destroy(self, window):
        GLib.source_remove(self.tick_id)
        if self.queue_refresh_id:
            GLib.source_remove(self.queue_refresh_id)
        for handler in self.queue_handlers:
            self.queue_model.disconnect(handler)
        for obj, handler in self.handlers:
            obj.disconnect(handler)
        self.lyrics_generation += 1
        screen = Gdk.Screen.get_default()
        Gtk.StyleContext.remove_provider_for_screen(screen, self.tint)


class NowPlayingPlugin(GObject.Object, Peas.Activatable):
    __gtype_name__ = "NowPlayingDisplayPlugin"
    object = GObject.Property(type=GObject.Object)

    def __init__(self):
        GObject.Object.__init__(self)
        self.window = None

    def do_activate(self):
        shell = self.object
        self.action = Gio.SimpleAction.new(ACTION, None)
        self.action.connect("activate", self.open_window)
        shell.props.window.add_action(self.action)

        item = Gio.MenuItem.new(label=_("Now Playing Display"), detailed_action="win." + ACTION)
        shell.props.application.add_plugin_menu_item("view", ACTION, item)

    def do_deactivate(self):
        shell = self.object
        shell.props.application.remove_plugin_menu_item("view", ACTION)
        shell.props.window.remove_action(ACTION)
        self.action = None
        if self.window is not None:
            self.window.destroy()
            self.window = None

    def open_window(self, action, parameter):
        if self.window is not None:
            self.window.present()
            return
        self.window = NowPlayingWindow(self.object)
        self.window.connect("destroy", self._window_closed)

    def _window_closed(self, window):
        self.window = None
