#!/usr/bin/env python3

# AGSImager: Make AGS entries

import json
import shutil
from sqlite3 import Connection
import sys
import textwrap
import operator

import ags_imgen as imgen
import ags_query as query
import ags_util as util

from ags_strings import strings
from ags_types import EntryCollection
from make_vadjust import VADJUST_MIN, VADJUST_MAX

AGS_INFO_WIDTH = 53
AGS_LIST_WIDTH = 26

# -----------------------------------------------------------------------------
# create entries from dictionary/tree

def make_tree(db: Connection, collection: EntryCollection, ags_path, node, path=[]):
    if isinstance(node, list):
        entries = []
        image = None
        note = None
        ordering = None
        rank = None

        for item in node:
            # plain titles
            if isinstance(item, str):
                entries += [item]
            if isinstance(item, list):
                if len(item) == 2:
                    entries += [(item[0], item[1])]
            # parse metadata or subtree
            if isinstance(item, dict):
                if "image" in item:
                    image = item.pop("image")
                if "note" in item:
                    note = str(item.pop("note"))
                if "ordering" in item:
                    ordering = item.pop("ordering")
                if "rank" in item:
                    rank = item.pop("rank")
                for key, value in item.items():
                    if isinstance(value, dict):
                        # item has override options
                        entries += [(key, value)]
                    else:
                        # item is a subtree
                        make_tree(db, collection, ags_path, value, path + [key])
        make_entries(db, collection, ags_path, entries, path, note=note, image=image, ordering=ordering, rank=rank)

# -----------------------------------------------------------------------------
# create entries from list

def make_entries(db: Connection, collection: EntryCollection, ags_path, entries, path, note=None, image=None, ordering=None, rank=None):
    # make dir
    base_path = ags_path
    if path:
        for d in path:
            base_path = util.path(base_path, d[:26].strip() + ".ags")
    util.make_dir(base_path)

    # set dir sort rank
    if rank is not None:
        collection.path_sort_rank[base_path] = rank

    # make note, image
    if note:
        note = "\n".join([textwrap.fill(p, AGS_INFO_WIDTH) for p in note.replace("\\n", "\n").splitlines()])
        open(base_path[:-4] + ".txt", mode="w", encoding="latin-1").write(note)
    if isinstance(image, (dict, list)):
        make_image(base_path[:-4] + ".iff", image)

    # collect titles
    pos = 0
    for name in entries:
        pos += 1
        n = name
        options = None
        if isinstance(name, tuple) and len(name) == 2 and isinstance(name[1], dict):
            n = name[0]
            options = name[1]

        # use preferred (fuzzy) entry
        e, pe = query.get_entry(db, n)
        if not "--" in name and pe:
            e = pe
        if not e and not pe:
            if options is None or (options and not options.get("unavailable", False)):
                print(" > WARNING: invalid entry: {}".format(n))
        else:
            collection.by_id[e["id"]] = e

        rank = str(pos).zfill(len(str(len(entries)))) if ordering == "ranked" else None

        sort_rank = None
        if ordering == "ordered": sort_rank = pos
        elif ordering == "release": sort_rank = util.parse_date_int(e["release_date"], sortable=True)

        make_entry(collection, ags_path, n, e, base_path, rank=rank, sort_rank=sort_rank, options=options)
    return

# -----------------------------------------------------------------------------
# create entry

def make_entry(entries: EntryCollection, ags_path, name, entry, path, rank=None, sort_rank=None, only_script=False, prefix=None, options=None):
    max_w = AGS_LIST_WIDTH
    note = None
    runfile_ext = "" if only_script else ".run"

    # apply override options
    if isinstance(options, dict):
        if entry and isinstance(entry, dict):
            entry.update(options)
        elif "note" in options and isinstance(options["note"], str):
            note = options["note"]

    if isinstance(entry, dict) and isinstance(entry["note"], str):
        note = entry["note"]

    # skip if entry already added at path
    path_id = json.dumps({"entry_id": entry["id"] if (entry and entry["id"]) else name, "path": path})
    if path_id in entries.path_ids:
        return
    else:
        entries.path_ids.add(path_id)

    # fix path name
    path_prefix = ags_path
    if path != path_prefix:
        path_suffix = path.split(path_prefix + "/")[-1]
        path = path_prefix + "/" + "/".join(list(map(sanitize_name, path_suffix.split("/"))))

    # base name
    title = rank + " " if rank else ""
    if prefix:
        title = prefix + " - " + title

    # prettify name
    if options and options.get("unavailable", False):
        title += sanitize_name(name.replace("-", " "))
    elif entry and "title_short" in entry:
        if only_script:
            title = sanitize_name(entry["title_short"]).replace(" ", "_")
        else:
            title += sanitize_name(entry["title_short"])
            if len(title) > max_w: title = title.replace(", The", "")
    else:
        title += name

    # prevent name clash
    title = title.strip()
    if util.is_file(util.path(path, title) + runfile_ext):
        if entry.get("category", "").lower() == "demo":
            title += " (" + entry.get("publisher") + ")"
        else:
            title += " (" + entry.get("hardware", "").replace("/ECS", "").replace("AGA/CD32", "CD32").replace("OCS/CDTV", "CDTV").replace("/", "-") + ")"
        if only_script:
            title = title.replace(" ", "_")
    if not only_script and len(title) > max_w:
        title = title[:max_w - 2].strip() + ".."
        if util.is_file(util.path(path, title) + runfile_ext):
            suffix = 1
            while suffix <= 10:
                title = title[:-1] + str(suffix)
                suffix += 1
                if not util.is_file(util.path(path, title) + runfile_ext):
                    break

    base_path = util.path(path, title)
    util.make_dir(path)

    # create runfile
    runfile = None
    if query.get_amiga_whd_dir(entry) is not None or query.entry_is_notwhdl(entry):
        # videomode
        whd_vmode = "NTSC" if util.parse_int(entry.get("ntsc", 0)) > 0 else "PAL"
        # vadjust
        vadjust_scale = util.parse_int(entry.get("scale", 0))
        if not vadjust_scale: vadjust_scale = 0
        vadjust_vofs = util.parse_int(entry.get("v_offset", 0))
        if not vadjust_vofs: vadjust_vofs = 0
        vadjust_vofs = min(max(vadjust_vofs, VADJUST_MIN), VADJUST_MAX)

        if query.entry_is_notwhdl(entry):
            runfile_source_path = query.get_archive_path(entry).replace(".lha", ".run")
            if util.is_file(runfile_source_path):
                runfile = "ags_notify TITLE=\"{}\"\n".format(entry.get("title", "Unknown"))
                runfile += "set{}\n".format(whd_vmode.lower())
                runfile += "setvadjust {} {}\n".format(vadjust_vofs, vadjust_scale)
                with open(runfile_source_path, 'r') as f: runfile += f.read()
                runfile += "setvmode $AGSVMode\n"
                runfile += "setvadjust\n"
                runfile += "ags_notify\n"
        else:
            whd_entrypath = query.get_amiga_whd_dir(entry)
            if whd_entrypath:
                whd_slave = query.get_whd_slavename(entry)
                # extra arguments
                whd_cargs = "BUTTONWAIT"
                if entry.get("slave_args"):
                    whd_cargs += " " + entry["slave_args"]
                whd_qtkey = "" if "QuitKey=" in whd_cargs else "$whdlqtkey"
                runfile = "ags_notify TITLE=\"{}\"\n".format(entry.get("title", "Unknown"))
                runfile += "cd \"{}\"\n".format(whd_entrypath)
                runfile += "IF NOT EXISTS ENV:whdlspdly\n"
                runfile += "  echo 200 >ENV:whdlspdly\n"
                runfile += "ENDIF\n"
                runfile += "IF NOT EXISTS ENV:whdlqtkey\n"
                runfile += "  echo \"\" >ENV:whdlqtkey\n"
                runfile += "ENDIF\n"
                runfile += "IF EXISTS ENV:whdlvmode\n"
                runfile += "  whdload >NIL: \"{}\" $whdlvmode {} SplashDelay=$whdlspdly {}\n".format(whd_slave, whd_cargs, whd_qtkey)
                runfile += "ELSE\n"
                runfile += "  setvadjust {} {}\n".format(vadjust_vofs, vadjust_scale)
                if only_script:
                    runfile += "  whdload >NIL: \"{}\" {} {} SplashDelay=0 {}\n".format(whd_slave, whd_vmode, whd_cargs, whd_qtkey)
                else:
                    runfile += "  whdload >NIL: \"{}\" {} {} SplashDelay=$whdlspdly {}\n".format(whd_slave, whd_vmode, whd_cargs, whd_qtkey)
                runfile += "  setvadjust\n"
                runfile += "ENDIF\n"
                runfile += "ags_notify\n"
    else:
        runfile = "echo \"Title not available.\"" + "\n" + "wait 2"

    if runfile:
        runfile_dest_path = base_path + runfile_ext
        if util.is_file(runfile_dest_path):
            print(" > AGS2 clash:", entry["id"], "-", runfile_dest_path)
        else:
            open(runfile_dest_path, mode="w", encoding="latin-1").write(runfile)

    if only_script:
        return None

    if options and "rank" in options:
        entries.path_sort_rank["{}.run".format(base_path)] = options["rank"]
    elif sort_rank is not None:
        entries.path_sort_rank["{}.run".format(base_path)] = sort_rank

    # note
    if options and options.get("unavailable", False):
        note = strings["note"]["title"] + name.replace("-", " ") + "\n\n" + strings["note"]["unavailable"]
        open(base_path + ".txt", mode="w", encoding="latin-1").write(note)
    elif entry:
        open(base_path + ".txt", mode="w", encoding="latin-1").write(make_note(entry, note))

    # image
    if entry and "image" in entry:
        make_image(base_path + ".iff", entry["image"])
    elif entry and "id" in entry and util.is_file(util.path("data", "img", entry["id"] + ".iff")):
        shutil.copyfile(util.path("data", "img", entry["id"] + ".iff"), base_path + ".iff")
    return base_path

# -----------------------------------------------------------------------------
# create note from entry

def make_note(entry, add_note):
    max_w = AGS_INFO_WIDTH
    note = ""
    system = entry["hardware"].replace("/", "·")
    aspect_ratio = "40:27"

    if entry.get("ntsc", 0) == 4:
        if entry.get("scale", 0) == 6:
            system += "·6×NTSC"
            aspect_ratio = "16:9"
        else:
            system += "·5×NTSC"
            aspect_ratio = "4:3"
    elif entry.get("ntsc", 0) == 3:
        if entry.get("scale", 0) == 6:
            system += "·6×NTSC"
            aspect_ratio = "16:9"
        else:
            system += "·5×NTSC"
    elif entry.get("ntsc", 0) == 2:
        if entry.get("scale", 0) == 6:
            system += "·6×NTSC"
            aspect_ratio = "16:9"
        else:
            system += "·5×NTSC"
    elif entry.get("ntsc", 0) == 1:
        if entry.get("scale", 0) == 6:
            system += "·6×PAL60"
            aspect_ratio = "16:9"
        else:
            system += "·5×PAL60"
    else:
        if entry.get("scale", 0) == 6:
            system += "·6×PAL"
            aspect_ratio = "16:9"
        elif entry.get("scale", 0) == 5:
            system += "·5×PAL"
        else:
            system += "·4×PAL"
            aspect_ratio = "4:3"

    if entry.get("lightgun", False):
        system += "·Light Gun"
    elif entry.get("gamepad", False):
        system += "·Game Pad"
    system += " ({})".format(aspect_ratio)

    if "category" in entry and entry["category"].lower() == "game":
        note += ("{}{}".format(strings["note"]["title"], entry["title"]))[:max_w] + "\n"
        note += ("{}{}".format(strings["note"]["developer"], entry["developer"]))[:max_w] + "\n"
        note += ("{}{}".format(strings["note"]["publisher"], entry["publisher"]))[:max_w] + "\n"
        note += ("{}{}".format(strings["note"]["release_date"], entry["release_date"]))[:max_w] + "\n"
        note += ("{}{}".format(strings["note"]["players"], entry["players"]))[:max_w] + "\n"
        note += ("{}{}".format(strings["note"]["system"], system))[:max_w] + "\n"
        if entry.get("issues"):
            note += ("{}{}".format(strings["note"]["issues"], entry["issues"]))[:max_w] + "\n"
        elif entry.get("hack"):
            note += ("{}{}".format(strings["note"]["hack"], entry["hack"]))[:max_w] + "\n"

    elif "category" in entry and entry["category"].lower() == "demo":
        group = util.prettify_names(entry["publisher"])
        note += ("{}{}".format(strings["note"]["title"], entry["title"]))[:max_w] + "\n"
        note += ("{}{}".format(strings["note"]["group"], group))[:max_w] + "\n"
        if entry.get("country"):
            note += ("{}{}".format(strings["note"]["country"], entry["country"]))[:max_w] + "\n"
        note += ("{}{}".format(strings["note"]["release_date"], entry["release_date"]))[:max_w] + "\n"
        if entry.get("subcategory", "").lower() != "demo":
            note += ("{}{}".format(strings["note"]["category"], entry["subcategory"]))[:max_w] + "\n"
        else:
            note += "{}Demo\n".format(strings["note"]["category"])
        note += ("{}{}".format(strings["note"]["system"], system))[:max_w] + "\n"
        if entry.get("issues"):
            note += ("{}{}".format(strings["note"]["issues"], entry["issues"]))[:max_w] + "\n"

    if add_note and isinstance(add_note, str):
            note += ("{}{}".format(strings["note"]["note"], add_note))[:max_w] + "\n"
    elif entry.get("note"):
            note += ("{}{}".format(strings["note"]["note"], entry["note"]))[:max_w] + "\n"
    return note

# -----------------------------------------------------------------------------

def sanitize_name(name):
    name = name.replace("/", "-").replace("\\", "-").replace(": ", " ").replace(":", " ")
    name = name.replace(" [AGA]", "")
    if name[0] == '(':
        name = name.replace('(', '[', 1).replace(')', ']', 1)
    return name

def make_image(path, options):
    if util.is_file(path):
        return
    imgen.out_iff(path, imgen.compose(options))

# -----------------------------------------------------------------------------
# collect entries for special folders ("All Games", "Demo Scene")

def make_autoentries(entries: EntryCollection, path, all_games=False, all_demos=False):
    d_path = util.path(path, "{}.ags".format(strings["dirs"]["scene"]))
    if all_demos: util.make_dir(d_path)

    for entry in sorted(entries.ids(), key=operator.itemgetter("title")):
        letter = entry.get("title_short", "z")[0].upper()
        if letter.isnumeric():
            letter = "#"
        year, _, _ = util.parse_date(entry["release_date"])
        year_img = year
        if "x" in year.lower():
            year = "Unknown"
            year_img = "19XX"

        # add games
        if all_games and entry.get("category", "").lower() == "game":
            make_entry(entries, path, None, entry, util.path(path, "{}.ags".format(strings["dirs"]["allgames"]), letter + ".ags"))
            make_image(util.path(path, "{}.ags".format(strings["dirs"]["allgames"]), letter + ".iff"), {"op":"tx", "txt": letter})
            make_entry(entries, path, None, entry, util.path(path, "{}.ags".format(strings["dirs"]["allgames_year"]), year + ".ags"))
            make_image(util.path(path, "{}.ags".format(strings["dirs"]["allgames_year"]), year + ".iff"), {"op":"tx", "txt": year_img, "size": 112})

        # add demos, disk mags
        def add_demo(entry, sort_group, sort_country):
            if sort_group.startswith("The "):
                sort_group = sort_group[4:]
            sort_group = sort_group[:AGS_LIST_WIDTH]
            group_letter = sort_group[0].upper()
            if group_letter.isnumeric():
                group_letter = "#"
            if entry.get("subcategory", "").lower().startswith("disk mag"):
                make_entry(entries, path, None, entry, util.path(d_path, "{}.ags".format(strings["dirs"]["diskmags"])))
                mag_path = make_entry(entries, path, None, entry, util.path(d_path, "{}.ags".format(strings["dirs"]["diskmags_date"])))
                if mag_path:
                    rank = util.parse_date_int(entry["release_date"], sortable=True)
                    entries.path_sort_rank["{}.run".format(mag_path)] = rank if rank else 0
            elif entry.get("subcategory", "").lower().startswith("music disk"):
                make_entry(entries, path, None, entry, util.path(d_path, "{}.ags".format(strings["dirs"]["musicdisks"]), letter + ".ags"))
                make_image(util.path(d_path, "{}.ags".format(strings["dirs"]["musicdisks"]), letter + ".iff"), {"op":"tx", "txt": letter})
                make_entry(entries, path, None, entry, util.path(d_path, "{}.ags".format(strings["dirs"]["musicdisks_year"]), year + ".ags"))
                make_image(util.path(d_path, "{}.ags".format(strings["dirs"]["musicdisks_year"]), year + ".iff"), {"op":"tx", "txt": year_img, "size": 112})
            else:
                if entry.get("subcategory", "").lower().startswith("crack"):
                    make_entry(entries, path, None, entry, util.path(d_path, "{}.ags".format(strings["dirs"]["demos_cracktro"])), prefix=sort_group)
                if entry.get("subcategory", "").lower().startswith("intro"):
                    make_entry(entries, path, None, entry, util.path(d_path, "{}.ags".format(strings["dirs"]["demos_intro"])))
                group_entry = dict(entry)
                group_entry["title_short"] = group_entry.get("title")
                make_entry(entries, path, None, entry, util.path(d_path, "{}.ags".format(strings["dirs"]["demos"]), letter + ".ags"))
                make_image(util.path(d_path, "{}.ags".format(strings["dirs"]["demos"]), letter + ".iff"), {"op":"tx", "txt": letter})
                make_entry(entries, path, None, group_entry, util.path(d_path, "{}.ags".format(strings["dirs"]["demos_group"]), group_letter + ".ags"), prefix=sort_group)
                make_image(util.path(d_path, "{}.ags".format(strings["dirs"]["demos_group"]), group_letter + ".iff"), {"op":"tx", "txt": group_letter})
                make_entry(entries, path, None, entry, util.path(d_path, "{}.ags".format(strings["dirs"]["demos_year"]), year + ".ags"))
                make_image(util.path(d_path, "{}.ags".format(strings["dirs"]["demos_year"]), year + ".iff"), {"op":"tx", "txt": year_img, "size": 112})
                if sort_country:
                    make_entry(entries, path, None, entry, util.path(d_path, "{}.ags".format(strings["dirs"]["demos_country"]), sort_country + ".ags"))

        if all_demos and entry.get("category", "").lower() == "demo":
            groups = entry.get("publisher")
            if not groups:
                continue
            for sort_group in groups.split(", "):
                countries = entry.get("country")
                if not countries:
                    add_demo(entry, sort_group, None)
                else:
                    for sort_country in countries.split(", "):
                        add_demo(entry, sort_group, sort_country)

        # run-scripts for randomizer
        if all_games and entry.get("category", "").lower() == "game" and not entry.get("issues"):
            make_entry(entries, path, None, entry, util.path(path, "Run", "Game"), only_script=True)
        elif all_demos and entry.get("category", "").lower() == "demo" and not entry.get("issues"):
            sub = entry.get("subcategory", "").lower()
            if sub.startswith("demo") or sub.startswith("intro") or sub.startswith("crack"):
                make_entry(entries, path, None, entry, util.path(path, "Run", "Demo"), only_script=True)

    # notes for created directories
    for dir in ["allgames", "allgames_year", "scene", "issues"]:
        if util.is_dir(util.path(path, "{}.ags".format(strings["dirs"][dir]))):
            open(util.path(path, "{}.txt".format(strings["dirs"][dir])), mode="w", encoding="latin-1").write(strings["desc"][dir])
    for dir in ["demos", "demos_country", "demos_group", "demos_year", "demos_cracktro", "demos_intro", "diskmags", "diskmags_date", "musicdisks", "musicdisks_year"]:
        if util.is_dir(util.path(d_path, "{}.ags".format(strings["dirs"][dir]))):
            open(util.path(d_path, "{}.txt".format(strings["dirs"][dir])), mode="w", encoding="latin-1").write(strings["desc"][dir])

# -----------------------------------------------------------------------------

if __name__ == "__main__":
    print("not runnable")
    sys.exit(1)