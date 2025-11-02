#!/usr/bin/env python3
# BOOSTED level setter
# - Edits CurrentLevel to DA_Level_<N> (same logic as working script)
# - Fixes nearby string length fields & payload-size
# - Also unlocks target level
# - Optional: boot via Menu instead of target (works around bad resume pointers)
# - Clears common resume/checkpoint tokens

import sys, struct, re
from pathlib import Path

def read_u32le(buf, off):
    return struct.unpack_from('<I', buf, off)[0]

def write_u32le(buf, off, val):
    struct.pack_into('<I', buf, off, val)

def find_near_len_field(buf, str_start, old_len, search_back=12):
    for back in range(4, search_back+1):
        off = str_start - back
        if off >= 0 and off + 4 <= len(buf):
            v = read_u32le(buf, off)
            if v == old_len:
                return off
    return None

def locate_current_level_regions(b: bytes):
    k = b.find(b'CurrentLevel')
    if k == -1:
        raise SystemExit("CurrentLevel not found.")
    prop_key = b.find(b'SoftObjectProperty', k)
    if prop_key == -1:
        raise SystemExit("SoftObjectProperty for CurrentLevel not found.")
    window_end = min(len(b), prop_key + 4096)
    seg = b[prop_key:window_end]

    # package path
    rel_path_start = seg.find(b'/Game/')
    if rel_path_start == -1:
        raise SystemExit("Package path '/Game/...' not found after property.")
    path_start = prop_key + rel_path_start
    path_end = b.find(b'\x00', path_start)
    if path_end == -1:
        raise SystemExit("Null terminator not found after package path.")
    old_path = b[path_start:path_end].decode('ascii', errors='strict')

    # short name
    rel_name_start = seg.find(b'DA_Level_', path_end - prop_key)
    if rel_name_start == -1:
        raise SystemExit("Short asset name 'DA_Level_*' not found after path.")
    name_start = prop_key + rel_name_start
    name_end = b.find(b'\x00', name_start)
    if name_end == -1:
        raise SystemExit("Null terminator not found after asset name.")
    old_name = b[name_start:name_end].decode('ascii', errors='strict')

    return (path_start, path_end, old_path), (name_start, name_end, old_name), prop_key

def patch_current_level_bytes(b: bytes, new_level_token: str) -> bytes:
    bb = bytearray(b)
    (path_start, path_end, old_path), (name_start, name_end, old_name), prop_key = locate_current_level_regions(bytes(bb))

    # Replace tokens in strings
    mp = re.search(r'DA_Level_[A-Za-z0-9]+', old_path)
    mn = re.search(r'DA_Level_[A-Za-z0-9]+', old_name)
    if not mp or not mn:
        raise SystemExit("DA_Level_* not found in CurrentLevel strings.")
    new_path = old_path[:mp.start()] + new_level_token + old_path[m.end():]
    new_name = new_level_token  # short name is just the token

    # Old/new lengths (include trailing nulls)
    old_path_len = (path_end - path_start) + 1
    old_name_len = (name_end - name_start) + 1
    new_path_len = len(new_path) + 1
    new_name_len = len(new_name) + 1
    delta_total = (new_path_len - old_path_len) + (new_name_len - old_name_len)

    # Rebuild buffer
    nb = bytearray()
    nb += bb[:path_start]
    nb += new_path.encode('ascii') + b'\x00'
    nb += bb[path_end+1:name_start]
    nb += new_name.encode('ascii') + b'\x00'
    nb += bb[name_end+1:]

    # Update nearby string-length fields
    new_path_start = path_start
    new_name_start = new_path_start + len(new_path) + 1 + (name_start - (path_end+1))
    p_off = find_near_len_field(nb, new_path_start, old_path_len)
    n_off = find_near_len_field(nb, new_name_start, old_name_len)
    if p_off is None or n_off is None:
        raise SystemExit("Failed to locate length fields for CurrentLevel strings.")
    write_u32le(nb, p_off, new_path_len)
    write_u32le(nb, n_off, new_name_len)

    # Bump a plausible payload-size u32 right after the type string
    after_type = prop_key + len('SoftObjectProperty')
    for i in range(0, 24, 4):
        off = after_type + i
        if off + 4 > len(nb): break
        v = read_u32le(nb, off)
        if v != 0:
            write_u32le(nb, off, v + delta_total)
            break

    return bytes(nb)

def unlock_level(b: bytes, level_token: str) -> bytes:
    bb = bytearray(b)
    token = level_token.encode('ascii') + b'\x00'
    i = 0
    flips = 0
    while True:
        j = bb.find(token, i)
        if j == -1: break
        start = max(0, j - 64)
        end = min(len(bb), j + 256)
        seg = bytes(bb[start:end])
        a = seg.find(b'bIsUnlocked')
        if a != -1:
            a2 = seg.find(b'BoolProperty', a)
            if a2 != -1:
                none = seg.find(b'None\x00', a2)
                if none == -1: none = len(seg)
                for k in range(a2 + 8, none):
                    if bb[start + k] == 0x00:
                        bb[start + k] = 0x01
                        flips += 1
                        break
        i = j + 1
    return bytes(bb)

def clear_resume(b: bytes) -> bytes:
    bb = bytearray(b)
    # Flip common run/resume flags to "not in a run"
    for key, to_one in ((b'CanResume', 0), (b'HasOngoingRun', 0), (b'IsInRun', 0)):
        i = 0
        while True:
            j = bb.find(key, i)
            if j == -1: break
            start = j; end = min(len(bb), j + 256)
            seg = bytes(bb[start:end])
            a2 = seg.find(b'BoolProperty')
            if a2 != -1:
                base = start + a2 + 8
                for k in range(base, min(end, base + 32)):
                    if bb[k] in (0x00, 0x01):
                        bb[k] = 0x01 if to_one else 0x00
                        break
            i = j + 1
    # Zero small arrays/counters that look like 'ResumeData', 'Checkpoint', 'CurrentRun'
    for key in (b'ResumeData', b'Checkpoint', b'CurrentRun'):
        i = 0
        while True:
            j = bb.find(key, i)
            if j == -1: break
            # look back a little to find a plausible u32 count and zero it
            for back in range(4, 16, 4):
                off = j - back
                if 0 <= off <= len(bb) - 4:
                    val = read_u32le(bb, off)
                    if 0 < val < 256:
                        write_u32le(bb, off, 0)
                        break
            i = j + 1
    return bytes(bb)

def main():
    print("-------- BOOSTED Level Editor --------\n")

    inp = input("Input save [BOOSTED.sav]: ").strip() or "BOOSTED.sav"
    inp_path = Path(inp)
    if not inp_path.exists():
        print("Not found:", inp_path); raise SystemExit(1)

    out_default = str(inp_path.with_name(inp_path.stem + "_patched" + inp_path.suffix))
    out_path = input(f"Output save [{out_default}]: ").strip() or out_default
    out = Path(out_path)

    lvl_s = input("Target level number (e.g., 4): ").strip()
    try:
        lvl = int(lvl_s)
    except Exception:
        print("Level must be an integer."); raise SystemExit(1)

    # Offer menu boot as a workaround for broken resume pointers
    menu_boot = input("Boot via Menu (and just unlock target)? y/N: ").strip().lower() == "y"

    token = f"DA_Level_{lvl}" if not menu_boot else "DA_Level_Menu"

    # Read & patch
    b = inp_path.read_bytes()
    b = patch_current_level_bytes(b, token)
    # Always unlock requested level number (even if menu boot selected)
    b = unlock_level(b, f"DA_Level_{lvl}")
    # Clear common resume tokens to avoid resuming into invalid state
    b = clear_resume(b)

    if out.exists():
        ans = input(f"{out} exists. Overwrite? [y/N]: ").strip().lower()
        if ans != "y":
            print("Aborted."); raise SystemExit(0)

    out.write_bytes(b)
    print(f"Saved: {out}")

if __name__ == '__main__':
    main()
