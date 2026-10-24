"""Build a SideStore/AltStore source from ipa/*.ipa into ./site (deployed to GitHub Pages by CI)."""
import io, json, os, plistlib, re, struct, subprocess, sys, zipfile, zlib
from pathlib import Path
from urllib.parse import quote

REPO = os.environ.get("GITHUB_REPOSITORY", "arnavaggarwal-dev/arnavs-ipas")
SHA = os.environ.get("GITHUB_SHA", "main")
OWNER, NAME = REPO.split("/")
PAGES = f"https://{OWNER.lower()}.github.io/{NAME}"


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
    z = zipfile.ZipFile(ipa)
    plist = info_plist(z)
    app_dir = plist[:-len("Info.plist")]
    p = plistlib.loads(z.read(plist))
    prefixes = tuple(p.get("CFBundleIcons", {}).get("CFBundlePrimaryIcon", {}).get("CFBundleIconFiles")
                     or p.get("CFBundleIconFiles") or ["AppIcon"])
    icons = [n for n in z.namelist() if n.startswith(app_dir) and n.endswith(".png")
             and "/" not in n[len(app_dir):] and n[len(app_dir):].startswith(prefixes)]
    icon = uncrush(z.read(max(icons, key=lambda n: z.getinfo(n).file_size))) if icons else None
    return z.getinfo(plist).date_time, p, icon


def main(out=Path("site")):
    (out / "icons").mkdir(parents=True, exist_ok=True)
    builds = sorted(((*read_ipa(f), f) for f in Path("ipa").glob("*.ipa")), key=lambda b: b[0], reverse=True)
    apps = {}
    for built, p, icon, ipa in builds:  # newest first, so newest build sets name/icon
        bid = p["CFBundleIdentifier"]
        name = p.get("CFBundleDisplayName") or p.get("CFBundleName") or ipa.stem
        if bid not in apps:
            if icon:
                (out / "icons" / f"{bid}.png").write_bytes(icon)
            apps[bid] = {
                "name": name,
                "bundleIdentifier": bid,
                "developerName": "Arnav",
                "localizedDescription": f"{name}, one of Arnav's games.",
                "iconURL": f"{PAGES}/icons/{bid}.png" if icon else "",
                # No appPermissions: SideStore rejects empty usage descriptions (Godot leaves them blank),
                # and v1 sources skip the install-time permission check.
                "versions": [],
            }
        v = {
            "version": p.get("CFBundleShortVersionString", "1.0"),
            "buildVersion": p.get("CFBundleVersion", "1"),
            "date": "%04d-%02d-%02d" % built[:3],
            "downloadURL": f"https://raw.githubusercontent.com/{REPO}/{SHA}/{quote(ipa.as_posix())}",
            "size": ipa.stat().st_size,
            "minOSVersion": p.get("MinimumOSVersion", "14.0"),
        }
        if all((o["version"], o["buildVersion"]) != (v["version"], v["buildVersion"]) for o in apps[bid]["versions"]):
            apps[bid]["versions"].append(v)
    for a in apps.values():  # legacy top-level fields for older SideStore/AltStore
        latest = a["versions"][0]
        a.update(version=latest["version"], versionDate=latest["date"], downloadURL=latest["downloadURL"], size=latest["size"])
    source = {
        "name": "Arnav's IPAs",
        "identifier": f"io.github.{OWNER.lower()}.{NAME.lower()}",
        "sourceURL": f"{PAGES}/source.json",
        "apps": list(apps.values()),
        "news": [],
    }
    (out / "source.json").write_text(json.dumps(source, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(apps)} apps -> {out / 'source.json'}")


def parse_version(v):
    return ([int(x) if x.isdigit() else 0 for x in str(v).split(".")] + [0, 0, 0])[:3]


def bump(ipa):
    """+0.0.1 on the IPA's version, counting from whichever is higher: the file or the last commit
    (so a fresh Godot export still at 1.0.0 lands above what's already published)."""
    with zipfile.ZipFile(ipa) as z:
        name = info_plist(z)
        raw = z.read(name)
        p = plistlib.loads(raw)
        versions = [p.get("CFBundleShortVersionString", "1.0.0")]
        head = subprocess.run(["git", "show", f"HEAD:{ipa.as_posix()}"], capture_output=True)
        if head.returncode == 0:
            versions.append(read_ipa(io.BytesIO(head.stdout))[1].get("CFBundleShortVersionString", "1.0.0"))
        base = max(map(parse_version, versions))
        new = f"{base[0]}.{base[1]}.{base[2] + 1}"
        p["CFBundleShortVersionString"] = p["CFBundleVersion"] = new
        fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
        tmp = ipa.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w") as out:
            for info in z.infolist():
                out.writestr(info, plistlib.dumps(p, fmt=fmt) if info.filename == name else z.read(info))
    tmp.replace(ipa)
    return f"{'.'.join(map(str, base))} -> {new}"


def update():
    """Interactive: pick IPAs by number, bump each by 0.0.1, commit and push."""
    os.chdir(Path(__file__).parent)
    ipas = sorted(Path("ipa").glob("*.ipa"))
    for i, f in enumerate(ipas, 1):
        print(f"{i}. {f.name} ({read_ipa(f)[1].get('CFBundleShortVersionString', '?')})")
    picks = {int(n) for n in input("Which to update (e.g. 1,2,3): ").split(",") if n.strip().isdigit()}
    picked = [ipas[n - 1] for n in sorted(picks) if 1 <= n <= len(ipas)]
    if not picked:
        sys.exit("Nothing picked.")
    changes = []
    for f in picked:
        changes.append(f"{f.stem} {bump(f)}")
        print(changes[-1])
    subprocess.run(["git", "add", "--", *map(str, picked)], check=True)
    subprocess.run(["git", "commit", "-m", "Update " + ", ".join(changes)], check=True)
    subprocess.run(["git", "push"], check=True)


if __name__ == "__main__":
    update() if sys.argv[1:] == ["update"] else main()
