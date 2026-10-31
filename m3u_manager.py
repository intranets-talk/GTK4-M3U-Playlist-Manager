#!/usr/bin/env python3
"""M3U Playlist Manager - a small GTK4 editor for IPTV .m3u files.

Debian 12:  sudo apt install python3-gi gir1.2-gtk-4.0
Run:        python3 m3u_manager.py [playlist.m3u]
"""
import hashlib
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

STD_ATTRS = ("group-title", "tvg-id", "tvg-name", "tvg-logo")
ATTR_RE = re.compile(r'([\w\-.]+)="([^"]*)"')

LOGO_W, LOGO_H = 96, 36          # size of the logo cell in the table
LOGO_MAX_BYTES = 3 * 1024 * 1024  # ignore anything bigger than this
LOGO_WORKERS = 6
LOGO_CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "m3u-manager", "logos")

# "Push" button: the playlist is copied with scp (key based auth, no password prompt).
# Edit this to match your SFTP connection
PUSH_TARGET = "user@xxx.xxx.xxx.xxx:/path/to/folder-with-m3u-files"
PUSH_TIMEOUT = 60  # seconds

# "From Server…": playlists in the same remote folder are downloaded to a local
# working copy, edited there, and sent back with Push.
PUSH_HOST, _sep, PUSH_DIR = PUSH_TARGET.partition(":")
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
REMOTE_CACHE_DIR = os.path.join(GLib.get_user_data_dir(), "m3u-manager", "remote")


# --------------------------------------------------------------------------
# M3U model / parser
# --------------------------------------------------------------------------
class Entry:
    def __init__(self, name="", url="", duration="-1", attrs=None, extras=None):
        self.name = name
        self.url = url
        self.duration = duration
        self.attrs = attrs if attrs is not None else {}
        self.extras = extras if extras is not None else []

    def to_lines(self):
        attrs = "".join(' %s="%s"' % (k, v) for k, v in self.attrs.items())
        head = "#EXTINF:%s%s,%s" % (self.duration, attrs, self.name)
        return [head] + list(self.extras) + [self.url]


def split_extinf(s):
    """Split '-1 tvg-id="a,b" group-title="x",Name' at the first comma outside quotes."""
    in_quotes = False
    for i, ch in enumerate(s):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == "," and not in_quotes:
            return s[:i], s[i + 1:]
    return s, ""


def parse_m3u(text):
    header = "#EXTM3U"
    entries = []
    pending = None
    stray = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("#EXTM3U"):
            header = line
        elif upper.startswith("#EXTINF:"):
            meta, name = split_extinf(line[8:])
            parts = meta.strip().split(None, 1)
            duration = parts[0] if parts else "-1"
            attrs = dict(ATTR_RE.findall(parts[1])) if len(parts) > 1 else {}
            pending = Entry(name.strip(), "", duration, attrs, stray)
            stray = []
        elif line.startswith("#"):
            # e.g. #EXTVLCOPT / #EXTGRP lines: keep them with their entry
            (pending.extras if pending else stray).append(line)
        else:
            if pending is None:  # bare URL without #EXTINF
                pending = Entry(line, line, extras=stray)
                stray = []
            pending.url = line
            entries.append(pending)
            pending = None
    return header, entries, stray


def read_text(path):
    with open(path, "rb") as f:
        data = f.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="M3U Playlist Manager")
        self.set_default_size(1150, 650)

        self.path = None
        self.header = "#EXTM3U"
        self.trailer = []
        self.db = {}          # uid -> Entry (rows only store the uid, so drag & drop is safe)
        self._uid = 0
        self._loading = False
        self._save_id = 0
        self._chooser = None
        self._note = ""

        self._pushing = False

        # logo cache: url -> GdkPixbuf (or None if it could not be loaded)
        self.logos = {}
        self._logo_pending = set()
        self._logo_queue = queue.LifoQueue()  # newest request (= visible row) first
        for _ in range(LOGO_WORKERS):
            threading.Thread(target=self._logo_worker, daemon=True).start()

        # columns: name, group, tvg-id, url, uid, logo
        self.store = Gtk.ListStore(str, str, str, str, int, str)
        for sig in ("row-inserted", "row-deleted", "row-changed"):
            self.store.connect(sig, self.on_model_changed)
        self.store.connect("rows-reordered", self.on_model_changed)

        self._build_ui()
        self._build_actions()
        self.connect("close-request", self.on_close)
        self._refresh_title()
        self._update_status()

    # ---- UI -------------------------------------------------------------
    def _build_ui(self):
        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        for label, action in (("Open…", "win.open"), ("Save As…", "win.save_as")):
            header.pack_start(Gtk.Button(label=label, action_name=action))
        header.pack_start(Gtk.Button(
            label="From Server…", action_name="win.open_remote",
            tooltip_text="Download a playlist from " + PUSH_TARGET + " and edit it"))
        header.pack_end(Gtk.Button(label="Save", action_name="win.save"))
        header.pack_end(Gtk.Button(label="Push", action_name="win.push",
                                   tooltip_text="Upload the playlist with scp to " + PUSH_TARGET))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(box)

        bar = Gtk.Box(spacing=6, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for label, action in (
            ("Add…", "win.add"),
            ("Edit…", "win.edit"),
            ("Remove", "win.remove"),
            ("Move Up", "win.up"),
            ("Move Down", "win.down"),
        ):
            bar.append(Gtk.Button(label=label, action_name=action))
        hint = Gtk.Label(
            label="Drag rows to reorder · type to search · double-click to edit",
            hexpand=True, xalign=1)
        hint.add_css_class("dim-label")
        bar.append(hint)
        box.append(bar)

        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_reorderable(True)
        self.tree.set_enable_search(True)
        self.tree.set_search_column(0)
        self.tree.set_headers_visible(True)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tree.connect("row-activated", lambda *a: self.on_edit())

        num_r = Gtk.CellRendererText(xalign=1.0)
        num_c = Gtk.TreeViewColumn("#")
        num_c.pack_start(num_r, False)
        num_c.set_cell_data_func(num_r, self._number_data, None)
        self.tree.append_column(num_c)

        pix_r = Gtk.CellRendererPixbuf()
        pix_r.set_fixed_size(LOGO_W, LOGO_H + 4)
        logo_c = Gtk.TreeViewColumn("Logo")
        logo_c.pack_start(pix_r, False)
        logo_c.set_cell_data_func(pix_r, self._logo_data, None)
        logo_c.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        logo_c.set_fixed_width(LOGO_W + 16)
        self.tree.append_column(logo_c)

        spec = (("Name", 0, 280), ("Group", 1, 170), ("tvg-id", 2, 150), ("URL", 3, 450))
        for title, idx, width in spec:
            r = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            c = Gtk.TreeViewColumn(title)
            c.pack_start(r, True)
            c.add_attribute(r, "text", idx)
            c.set_resizable(True)
            c.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            c.set_fixed_width(width)
            self.tree.append_column(c)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.tree.add_controller(keys)

        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroll.set_child(self.tree)
        box.append(scroll)

        self.status = Gtk.Label(xalign=0, margin_top=4, margin_bottom=4,
                                margin_start=8, margin_end=8)
        self.status.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        box.append(self.status)

    @staticmethod
    def _number_data(column, cell, model, it, data):
        cell.set_property("text", str(model.get_path(it).get_indices()[0] + 1))

    # ---- logos ----------------------------------------------------------
    # The tree only asks for cells that are on screen, so logos are fetched
    # lazily while you scroll. Downloads run in background threads and are
    # cached on disk (~/.cache/m3u-manager/logos).
    def _logo_data(self, column, cell, model, it, data):
        url = model.get_value(it, 5)
        pix = None
        if url:
            if url in self.logos:
                pix = self.logos[url]
            elif url not in self._logo_pending:
                self._logo_pending.add(url)
                self._logo_queue.put(url)
        cell.set_property("pixbuf", pix)

    def _logo_worker(self):
        while True:
            url = self._logo_queue.get()
            pix = None
            try:
                data = self._logo_bytes(url)
                stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
                pix = GdkPixbuf.Pixbuf.new_from_stream_at_scale(
                    stream, LOGO_W, LOGO_H, True, None)
            except Exception:
                pix = None
            GLib.idle_add(self._logo_done, url, pix)

    @staticmethod
    def _logo_bytes(url):
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("unsupported logo URL")
        os.makedirs(LOGO_CACHE_DIR, exist_ok=True)
        cached = os.path.join(LOGO_CACHE_DIR, hashlib.sha1(url.encode("utf-8")).hexdigest())
        if os.path.exists(cached):
            with open(cached, "rb") as f:
                return f.read()
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (M3U Manager)"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = resp.read(LOGO_MAX_BYTES + 1)
        if not data or len(data) > LOGO_MAX_BYTES:
            raise ValueError("bad logo size")
        tmp = cached + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, cached)
        return data

    def _logo_done(self, url, pix):
        self._logo_pending.discard(url)
        self.logos[url] = pix
        self.tree.queue_draw()
        return GLib.SOURCE_REMOVE

    def _build_actions(self):
        for name, cb in (
            ("open", self.on_open), ("save", self.on_save), ("save_as", self.on_save_as),
            ("push", self.on_push), ("open_remote", self.on_open_remote),
            ("add", self.on_add), ("edit", self.on_edit), ("remove", self.on_remove),
            ("up", lambda *a: self.move(-1)), ("down", lambda *a: self.move(1)),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

    def show_error(self, msg, parent=None):
        dlg = Gtk.MessageDialog(transient_for=parent or self, modal=True,
                                message_type=Gtk.MessageType.ERROR,
                                buttons=Gtk.ButtonsType.CLOSE, text=msg)
        dlg.connect("response", lambda d, r: d.destroy())
        dlg.present()

    def _refresh_title(self):
        name = os.path.basename(self.path) if self.path else "No file"
        self.set_title("%s – M3U Playlist Manager" % name)

    def _update_status(self):
        self.status.set_text("%d entries — %s%s" % (
            len(self.store), self.path or "no file open", self._note))

    # ---- helpers --------------------------------------------------------
    def _new_uid(self):
        self._uid += 1
        return self._uid

    @staticmethod
    def _row(entry, uid):
        return [entry.name, entry.attrs.get("group-title", ""),
                entry.attrs.get("tvg-id", ""), entry.url, uid,
                entry.attrs.get("tvg-logo", "")]

    def _selected_indices(self):
        _, paths = self.tree.get_selection().get_selected_rows()
        return sorted(p.get_indices()[0] for p in paths)

    def _select_index(self, i):
        if len(self.store) == 0:
            return
        i = max(0, min(len(self.store) - 1, i))
        path = Gtk.TreePath.new_from_indices([i])
        sel = self.tree.get_selection()
        sel.unselect_all()
        sel.select_path(path)
        self.tree.scroll_to_cell(path, None, False, 0, 0)

    # ---- load / save ----------------------------------------------------
    def load_file(self, path):
        try:
            text = read_text(path)
        except OSError as e:
            self.show_error("Cannot open file:\n%s" % e)
            return
        header, entries, trailer = parse_m3u(text)

        if self._save_id:  # write any pending edits of the current file first
            GLib.source_remove(self._save_id)
            self._do_save()
        try:  # safety copy of the file as it was when opened
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass

        self._loading = True
        self.store.clear()
        self.db.clear()
        for e in entries:
            uid = self._new_uid()
            self.db[uid] = e
            self.store.append(self._row(e, uid))
        self._loading = False

        self.path, self.header, self.trailer = path, header, trailer
        self._note = ""
        self._refresh_title()
        self._update_status()

    def write_file(self, path):
        lines = [self.header]
        for row in self.store:
            lines.extend(self.db[row[4]].to_lines())
        lines.extend(self.trailer)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, path)

    def _do_save(self):
        self._save_id = 0
        if not self.path:
            return GLib.SOURCE_REMOVE
        try:
            self.write_file(self.path)
            self._note = " — saved " + GLib.DateTime.new_now_local().format("%H:%M:%S")
        except OSError as e:
            self._note = " — SAVE FAILED"
            self.show_error("Cannot save file:\n%s" % e)
        self._update_status()
        return GLib.SOURCE_REMOVE

    def _queue_save(self):
        """Autosave shortly after the last change (drag & drop fires several signals)."""
        if not self.path:
            return
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._save_id = GLib.timeout_add(400, self._do_save)

    def on_model_changed(self, *args):
        if self._loading:
            return
        self._update_status()
        self._queue_save()

    def on_close(self, *args):
        if self._save_id:
            GLib.source_remove(self._save_id)
            self._do_save()
        return False

    # ---- file chooser actions ------------------------------------------
    def _make_chooser(self, title, action, accept):
        ch = Gtk.FileChooserNative.new(title, self, action, accept, "Cancel")
        flt = Gtk.FileFilter()
        flt.set_name("M3U playlists")
        flt.add_suffix("m3u")
        flt.add_suffix("m3u8")
        ch.add_filter(flt)
        allf = Gtk.FileFilter()
        allf.set_name("All files")
        allf.add_pattern("*")
        ch.add_filter(allf)
        self._chooser = ch  # keep a reference or it gets garbage collected
        return ch

    def on_open(self, *args):
        ch = self._make_chooser("Open M3U playlist", Gtk.FileChooserAction.OPEN, "Open")

        def done(d, resp):
            if resp == Gtk.ResponseType.ACCEPT:
                self.load_file(d.get_file().get_path())
        ch.connect("response", done)
        ch.show()

    def on_save(self, *args):
        if not self.path:
            self.on_save_as()
            return
        if self._save_id:
            GLib.source_remove(self._save_id)
        self._do_save()

    def on_save_as(self, *args):
        ch = self._make_chooser("Save playlist as", Gtk.FileChooserAction.SAVE, "Save")
        ch.set_current_name(os.path.basename(self.path) if self.path else "playlist.m3u")

        def done(d, resp):
            if resp == Gtk.ResponseType.ACCEPT:
                self.path = d.get_file().get_path()
                self._refresh_title()
                self._do_save()
        ch.connect("response", done)
        ch.show()

    # ---- open from server -----------------------------------------------
    def on_open_remote(self, *args):
        dlg = Gtk.Dialog(title="Open from server", transient_for=self, modal=True,
                         use_header_bar=True)
        dlg.set_default_size(440, 420)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        info = Gtk.Label(label="Loading file list from %s …" % PUSH_TARGET, xalign=0,
                         margin_top=8, margin_bottom=8, margin_start=12, margin_end=12)
        info.set_wrap(True)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True, min_content_height=300)
        scroll.set_child(listbox)
        area = dlg.get_content_area()
        area.append(info)
        area.append(scroll)

        alive = [True]

        def on_response(d, resp):
            alive[0] = False
            d.destroy()
        dlg.connect("response", on_response)

        def on_row(lb, row):
            alive[0] = False
            dlg.destroy()
            self._download_remote(row.remote_name)
        listbox.connect("row-activated", on_row)

        def fill(err, names):
            if not alive[0]:
                return GLib.SOURCE_REMOVE
            if err:
                info.set_text("Could not list files:\n%s" % err)
            elif not names:
                info.set_text("No .m3u / .m3u8 files found in %s" % PUSH_DIR)
            else:
                info.set_text("Double-click a playlist to open it:")
                for name in names:
                    row = Gtk.ListBoxRow()
                    row.remote_name = name
                    row.set_child(Gtk.Label(label=name, xalign=0, margin_top=6,
                                            margin_bottom=6, margin_start=10, margin_end=10))
                    listbox.append(row)
            return GLib.SOURCE_REMOVE

        def worker():
            remote_cmd = ("find %s -maxdepth 1 -type f \\( -iname '*.m3u' -o -iname '*.m3u8' \\) "
                          "-printf '%%f\\n' | sort" % shlex.quote(PUSH_DIR))
            err, names = None, []
            try:
                res = subprocess.run(["ssh"] + SSH_OPTS + [PUSH_HOST, remote_cmd],
                                     capture_output=True, text=True, timeout=PUSH_TIMEOUT)
                if res.returncode != 0:
                    err = (res.stderr or res.stdout).strip() or "ssh failed"
                else:
                    names = [l for l in res.stdout.splitlines() if l.strip()]
            except FileNotFoundError:
                err = "ssh not found. Install it with: sudo apt install openssh-client"
            except subprocess.TimeoutExpired:
                err = "Timed out after %d seconds." % PUSH_TIMEOUT
            GLib.idle_add(fill, err, names)

        threading.Thread(target=worker, daemon=True).start()
        dlg.present()

    def _download_remote(self, name):
        name = os.path.basename(name)
        local = os.path.join(REMOTE_CACHE_DIR, name)
        remote = "%s:%s/%s" % (PUSH_HOST, PUSH_DIR.rstrip("/"), name)
        self._note = " — downloading…"
        self._update_status()

        def worker():
            err = None
            try:
                os.makedirs(REMOTE_CACHE_DIR, exist_ok=True)
                if os.path.exists(local):  # keep the previous working copy, just in case
                    shutil.copy2(local, local + ".prev")
                tmp = local + ".part"
                res = subprocess.run(["scp", "-q"] + SSH_OPTS + [remote, tmp],
                                     capture_output=True, text=True, timeout=PUSH_TIMEOUT)
                if res.returncode != 0:
                    raise RuntimeError((res.stderr or res.stdout).strip() or "scp failed")
                os.replace(tmp, local)
            except FileNotFoundError:
                err = "scp not found. Install it with: sudo apt install openssh-client"
            except subprocess.TimeoutExpired:
                err = "Timed out after %d seconds." % PUSH_TIMEOUT
            except (OSError, RuntimeError) as e:
                err = str(e)
            GLib.idle_add(self._download_done, local, err)

        threading.Thread(target=worker, daemon=True).start()

    def _download_done(self, local, err):
        if err:
            self._note = " — DOWNLOAD FAILED"
            self._update_status()
            self.show_error("Download failed:\n%s" % err)
        else:
            self.load_file(local)
            self._note = " — downloaded from server (Push sends it back)"
            self._update_status()
        return GLib.SOURCE_REMOVE

    # ---- push to server -------------------------------------------------
    def on_push(self, *args):
        if self._pushing:
            return
        if not self.path:
            self.show_error("Open or save a playlist first.")
            return
        # make sure the file on disk has the latest edits before uploading
        if self._save_id:
            GLib.source_remove(self._save_id)
            self._save_id = 0
        try:
            self.write_file(self.path)
        except OSError as e:
            self.show_error("Cannot save file:\n%s" % e)
            return
        self._pushing = True
        self.lookup_action("push").set_enabled(False)
        self._note = " — pushing…"
        self._update_status()
        threading.Thread(target=self._push_worker, args=(self.path,), daemon=True).start()

    def _push_worker(self, path):
        # same as: scp file root@host:/dir  (BatchMode = never wait for a password prompt)
        cmd = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
               path, PUSH_TARGET]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=PUSH_TIMEOUT)
            ok = res.returncode == 0
            msg = (res.stderr or res.stdout).strip()
        except FileNotFoundError:
            ok, msg = False, "scp not found. Install it with: sudo apt install openssh-client"
        except subprocess.TimeoutExpired:
            ok, msg = False, "Timed out after %d seconds." % PUSH_TIMEOUT
        GLib.idle_add(self._push_done, ok, msg)

    def _push_done(self, ok, msg):
        self._pushing = False
        self.lookup_action("push").set_enabled(True)
        if ok:
            self._note = " — pushed " + GLib.DateTime.new_now_local().format("%H:%M:%S")
        else:
            self._note = " — PUSH FAILED"
            self.show_error("Push failed:\n%s" % (msg or "unknown error"))
        self._update_status()
        return GLib.SOURCE_REMOVE

    # ---- editing --------------------------------------------------------
    def entry_dialog(self, entry, title, on_ok):
        dlg = Gtk.Dialog(title=title, transient_for=self, modal=True, use_header_bar=True)
        dlg.set_default_size(600, -1)
        dlg.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("OK", Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)

        grid = Gtk.Grid(column_spacing=12, row_spacing=8, margin_top=12,
                        margin_bottom=12, margin_start=12, margin_end=12)
        fields = {}
        rows = (("Name", "name"), ("URL", "url"), ("Group", "group-title"),
                ("tvg-id", "tvg-id"), ("tvg-name", "tvg-name"), ("Logo URL", "tvg-logo"))
        for i, (label, key) in enumerate(rows):
            if key == "name":
                value = entry.name
            elif key == "url":
                value = entry.url
            else:
                value = entry.attrs.get(key, "")
            e = Gtk.Entry(hexpand=True, activates_default=True)
            e.set_text(value)
            fields[key] = e
            grid.attach(Gtk.Label(label=label, xalign=1), 0, i, 1, 1)
            grid.attach(e, 1, i, 1, 1)

        extra_view = Gtk.TextView()
        extra_view.get_buffer().set_text("\n".join(entry.extras))
        extra_scroll = Gtk.ScrolledWindow(min_content_height=70, hexpand=True)
        extra_scroll.set_child(extra_view)
        extra_scroll.set_has_frame(True)
        lbl = Gtk.Label(label="Extra lines", xalign=1, yalign=0)
        grid.attach(lbl, 0, len(rows), 1, 1)
        grid.attach(extra_scroll, 1, len(rows), 1, 1)
        dlg.get_content_area().append(grid)

        def response(d, resp):
            if resp == Gtk.ResponseType.OK:
                name = fields["name"].get_text().strip()
                url = fields["url"].get_text().strip()
                if not url:
                    self.show_error("The URL cannot be empty.", parent=d)
                    return
                buf = extra_view.get_buffer()
                extras = [l.strip() for l in buf.get_text(
                    buf.get_start_iter(), buf.get_end_iter(), False).splitlines() if l.strip()]
                entry.name = name or url
                entry.url = url
                entry.extras = extras
                for k in STD_ATTRS:
                    v = fields[k].get_text().replace('"', "'").strip()
                    if v:
                        entry.attrs[k] = v
                    else:
                        entry.attrs.pop(k, None)
                on_ok()
            d.destroy()

        dlg.connect("response", response)
        dlg.present()
        fields["name"].grab_focus()

    def on_add(self, *args):
        entry = Entry()
        idx = self._selected_indices()
        pos = idx[-1] + 1 if idx else len(self.store)

        def ok():
            uid = self._new_uid()
            self.db[uid] = entry
            self.store.insert(pos, self._row(entry, uid))
            self._select_index(pos)

        self.entry_dialog(entry, "Add channel", ok)

    def on_edit(self, *args):
        idx = self._selected_indices()
        if not idx:
            return
        row = self.store[Gtk.TreePath.new_from_indices([idx[0]])]
        entry = self.db[row[4]]

        def ok():
            vals = self._row(entry, row[4])
            for col in (0, 1, 2, 3, 5):
                row[col] = vals[col]

        self.entry_dialog(entry, "Edit channel", ok)

    def on_remove(self, *args):
        idx = self._selected_indices()
        for i in reversed(idx):
            self.store.remove(self.store.get_iter(Gtk.TreePath.new_from_indices([i])))
        if idx:
            self._select_index(idx[0])

    def move(self, step):
        sel = self.tree.get_selection()
        idx = self._selected_indices()
        n = len(self.store)
        for i in (idx if step < 0 else reversed(idx)):
            j = i + step
            if j < 0 or j >= n:
                continue
            if sel.path_is_selected(Gtk.TreePath.new_from_indices([j])):
                continue
            self.store.swap(self.store.get_iter(Gtk.TreePath.new_from_indices([i])),
                            self.store.get_iter(Gtk.TreePath.new_from_indices([j])))
        if len(idx) == 1:
            j = max(0, min(n - 1, idx[0] + step))
            self.tree.scroll_to_cell(Gtk.TreePath.new_from_indices([j]), None, False, 0, 0)

    def on_key(self, ctrl, keyval, keycode, state):
        if keyval == Gdk.KEY_Delete:
            self.on_remove()
            return True
        return False


# --------------------------------------------------------------------------
class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.example.M3UManager",
                         flags=Gio.ApplicationFlags.HANDLES_OPEN)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        # Force a dark look regardless of what GTK4 picks up from the MATE theme.
        Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
        Gtk.Window.set_default_icon_name("m3u-manager")
        for action, accel in (
            ("win.open", "<Primary>o"), ("win.save", "<Primary>s"),
            ("win.save_as", "<Primary><Shift>s"), ("win.add", "<Primary>n"),
            ("win.edit", "<Primary>e"), ("win.up", "<Alt>Up"), ("win.down", "<Alt>Down"),
            ("win.push", "<Primary>u"), ("win.open_remote", "<Primary><Shift>o"),
        ):
            self.set_accels_for_action(action, [accel])

    def do_activate(self):
        Window(self).present()

    def do_open(self, files, n_files, hint):
        for f in files:
            path = f.get_path()
            if path:
                win = Window(self)
                win.present()
                win.load_file(path)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
