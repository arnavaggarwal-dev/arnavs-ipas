"""Publish picked ipa/*.ipa to GitHub Releases and update the SideStore source in docs/ (served by GitHub Pages)."""
import datetime, json, os, plistlib, re, struct, subprocess, sys, zipfile, zlib
from pathlib import Path

REPO = "arnavaggarwal-dev/arnavs-ipas"
OWNER, NAME = REPO.split("/")
PAGES = f"https://{OWNER.lower()}.github.io/{NAME}"
DOCS = Path("docs")


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def uncrush(data):
    """Xcode 'CgBI' PNG (BGRA, headerless deflate) -> normal PNG. Normal PNGs pass through."""
    chunks, i = {}, 8
    while i < len(data):
        n, kind = struct.unpack(">I4s", data[i:i + 8])
        chunks.setdefault(kind, []).append(data[i + 8:i + 8 + n])
        i += 12 + n
    if b"CgBI" not in chunks:
        return data
    ihdr = chunks[b"IHDR"][0]
    w, h = struct.unpack(">II", ihdr[:8])
    raw = bytearray(zlib.decompress(b"".join(chunks[b"IDAT"]), -15))
    # PNG filters work per channel, so swapping B/R on filtered rows is safe.
    # ponytail: skips un-premultiplying alpha, only visible on translucent icon edges
    for y in range(h):
        r = y * (1 + w * 4) + 1
        raw[r:r + w * 4:4], raw[r + 2:r + w * 4:4] = raw[r + 2:r + w * 4:4], raw[r:r + w * 4:4]
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def info_plist(z):
    return next(n for n in z.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n))


def read_ipa(ipa):
    with zipfile.ZipFile(ipa) as z:
        plist = info_plist(z)
        app_dir = plist[:-len("Info.plist")]
        p = plistlib.loads(z.read(plist))
        prefixes = tuple(p.get("CFBundleIcons", {}).get("CFBundlePrimaryIcon", {}).get("CFBundleIconFiles")
                         or p.get("CFBundleIconFiles") or ["AppIcon"])
        icons = [n for n in z.namelist() if n.startswith(app_dir) and n.endswith(".png")
                 and "/" not in n[len(app_dir):] and n[len(app_dir):].startswith(prefixes)]
        icon = uncrush(z.read(max(icons, key=lambda n: z.getinfo(n).file_size))) if icons else None
    return p, icon


def parse_version(v):
    return ([int(x) if x.isdigit() else 0 for x in str(v).split(".")] + [0, 0, 0])[:3]


def bump(ipa, live):
    """+0.0.1 over whichever is higher, the file or the live store version
    (so a fresh Godot export still at 1.0.0 lands above what's published)."""
    with zipfile.ZipFile(ipa) as z:
        name = info_plist(z)
        raw = z.read(name)
        p = plistlib.loads(raw)
        base = max(parse_version(p.get("CFBundleShortVersionString", "1.0.0")), parse_version(live))
        p["CFBundleShortVersionString"] = p["CFBundleVersion"] = f"{base[0]}.{base[1]}.{base[2] + 1}"
        fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
        tmp = ipa.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w") as out:
            for info in z.infolist():
                out.writestr(info, plistlib.dumps(p, fmt=fmt) if info.filename == name else z.read(info))
    tmp.replace(ipa)


def upload(ipa, tag):
    """One release per app version; returns the asset's download URL."""
    if subprocess.run(["gh", "release", "create", tag, str(ipa), "-R", REPO, "--title", tag, "--notes", tag]).returncode:
        subprocess.run(["gh", "release", "upload", tag, str(ipa), "-R", REPO, "--clobber"], check=True)
    out = subprocess.run(["gh", "release", "view", tag, "-R", REPO, "--json", "assets"], capture_output=True, check=True).stdout
    url = next((a["url"] for a in json.loads(out)["assets"] if a["size"] == ipa.stat().st_size), None)
    if not url:
        sys.exit(f"{tag}: uploaded file size doesn't match {ipa.name}")
    return url


def git_push(message):
    subprocess.run(["git", "add", "-A"], check=True)
    subprocess.run(["git", "commit", "-m", message])  # fine if there's nothing new
    subprocess.run(["git", "push"], check=True)


def main():
    os.chdir(Path(__file__).parent)
    src_file = DOCS / "source.json"
    source = json.loads(src_file.read_text(encoding="utf-8")) if src_file.exists() else {
        "name": "Arnav's IPAs",
        "identifier": f"io.github.{OWNER.lower()}.{NAME.lower()}",
        "sourceURL": f"{PAGES}/source.json",
        "apps": [],
        "news": [],
    }
    apps = {a["bundleIdentifier"]: a for a in source["apps"]}
    ipas = sorted(Path("ipa").glob("*.ipa"))
    new = []
    for i, f in enumerate(ipas, 1):
        p = read_ipa(f)[0]
        live = apps.get(p["CFBundleIdentifier"], {}).get("version")
        if not live:
            new.append(f)
        print(f"{i}. {f.name} ({p.get('CFBundleShortVersionString', '?')}, {'live ' + live if live else 'new'})")
    answer = input("Which to update (e.g. 1,2,3; blank = publish the new ones and push): ")
    if answer.strip():
        picks = {int(n) for n in answer.split(",") if n.strip().isdigit()}
        picked = [ipas[n - 1] for n in sorted(picks) if 1 <= n <= len(ipas)]
        if not picked:
            sys.exit("Nothing picked.")
    elif new:
        picked = new
    else:
        return git_push("Sync")

    changes = []
    for f in picked:
        bid = read_ipa(f)[0]["CFBundleIdentifier"]
        if bid in apps:  # new apps go out at their own version
            bump(f, apps[bid]["version"])
        p, icon = read_ipa(f)
        version = p.get("CFBundleShortVersionString", "1.0")
        name = p.get("CFBundleDisplayName") or p.get("CFBundleName") or f.stem
        print(f"Uploading {name} {version}...")
        url = upload(f, re.sub(r"[^\w.-]+", "-", f"{f.stem}-{version}"))
        if icon:
            (DOCS / "icons").mkdir(parents=True, exist_ok=True)
            (DOCS / "icons" / f"{bid}.png").write_bytes(icon)
        v = {
            "version": version,
            "buildVersion": p.get("CFBundleVersion", version),
            "date": datetime.date.today().isoformat(),
            "downloadURL": url,
            "size": f.stat().st_size,
            "minOSVersion": p.get("MinimumOSVersion", "14.0"),
        }
        apps[bid] = {
            "name": name,
            "bundleIdentifier": bid,
            "developerName": "Arnav",
            "localizedDescription": apps.get(bid, {}).get("localizedDescription", f"{name}, one of Arnav's games."),
            "iconURL": f"{PAGES}/icons/{bid}.png",
            # No appPermissions: SideStore rejects empty usage descriptions (Godot leaves them blank),
            # and v1 sources skip the install-time permission check.
            "versions": [v],
            # Legacy top-level fields for older SideStore/AltStore.
            "version": version, "versionDate": v["date"], "downloadURL": url, "size": v["size"],
        }
        changes.append(f"{name} {version}")

    source["apps"] = list(apps.values())
    src_file.write_text(json.dumps(source, indent=2, ensure_ascii=False), encoding="utf-8")
    git_push("Publish " + ", ".join(changes))


if __name__ == "__main__":
    main()
