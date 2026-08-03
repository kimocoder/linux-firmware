#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR MIT
"""
update_firmware.py - Sync & update firmware blobs from upstream linux-firmware.

This tool automatically fetches the latest upstream linux-firmware repository,
filters WHENCE and firmware files to retain ONLY the firmwares needed by this project
(Wi-Fi, Bluetooth, USB serial, SDR, DVB, TV tuners, etc.), updates binary blobs,
symlinks definitions in WHENCE, and license texts, and strips out all unneeded files.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

# Standard repository tooling and non-firmware files to preserve
TOOLING_FILES = {
    "AGENTS.md",
    ".codespell.cfg",
    ".editorconfig",
    ".gitignore",
    ".gitlab-ci.yml",
    ".pre-commit-config.yaml",
    "Dockerfile",
    "Makefile",
    "LICENSE",
    "README.md",
    "WHENCE",
    "build_packages.py",
    "check_whence.py",
    "update_firmware.py",
    "copy-firmware.sh",
    "dedup-firmware.sh",
}

TOOLING_PREFIXES = (
    ".git/",
    ".github/",
    "contrib/",
)

DEFAULT_UPSTREAM_URL = "https://gitlab.com/kernel-firmware/linux-firmware.git"


def parse_whence_blocks(whence_path):
    """
    Parse WHENCE file into header and driver blocks.
    Returns (header_str, list_of_block_strings).
    """
    with open(whence_path, "r", encoding="utf-8") as f:
        content = f.read()

    raw_blocks = re.split(r"\n-{10,}\n", content)
    header = raw_blocks[0] if raw_blocks else ""
    blocks = raw_blocks[1:] if len(raw_blocks) > 1 else []
    return header, blocks


def extract_block_entries(block_str):
    """
    Extract files, links, sources, driver names, and license references from a block string.
    Returns (files, links, sources, licenses, drivers).
    """
    files = set()
    links = set()
    sources = set()
    licenses = set()
    drivers = set()

    for line in block_str.splitlines():
        line_s = line.strip()
        if line_s.startswith("Driver:"):
            d_name = line_s.replace("Driver:", "").strip().split("-")[0].strip()
            if d_name:
                drivers.add(d_name)
            continue

        # Match File: or RawFile:
        m = re.match(r'(?:RawFile|File):\s*"(.*)"', line_s)
        if not m:
            m = re.match(r"(?:RawFile|File):\s*(\S*)", line_s)
        if m:
            path = m.group(1).replace(r"\ ", " ").replace('"', "").strip()
            files.add(path)
            continue

        # Match Source:
        m = re.match(r'Source:\s*"(.*)"', line_s)
        if not m:
            m = re.match(r"Source:\s*(\S*)", line_s)
        if m:
            path = m.group(1).replace(r"\ ", " ").replace('"', "").strip()
            sources.add(path)
            continue

        # Match Link:
        m = re.match(r"Link:\s*(.*)", line_s)
        if m:
            parts = m.group(1).split("->")
            if len(parts) == 2:
                linkname = parts[0].strip().replace(r"\ ", " ").replace('"', "")
                target = parts[1].strip().replace(r"\ ", " ").replace('"', "")
                links.add((linkname, target))
            continue

        # Match Licence / License lines
        m = re.search(r"Licen[cs]e:\s*(.*)", line_s)
        if m:
            lic_text = m.group(1)
            for ref in re.findall(r"\bSee\s+(\S+)\s+for details", lic_text, re.IGNORECASE):
                ref = ref.strip().rstrip(".")
                if ref and "/" not in ref:
                    licenses.add(ref)
            for ref in re.findall(r"\bSee\s+(LICEN[CS]E\S*)", lic_text, re.IGNORECASE):
                ref = ref.strip().rstrip(".")
                if ref and "/" not in ref:
                    licenses.add(ref)

    return files, links, sources, licenses, drivers


def get_baseline_rules(local_whence):
    """
    Get baseline paths and driver names from current local WHENCE file.
    """
    files, links, sources, licenses, drivers = extract_block_entries(open(local_whence, "r", encoding="utf-8").read())
    baseline_paths = files | sources | set(l[0] for l in links)
    return baseline_paths, drivers


def sync_firmware(upstream_dir, repo_dir, dry_run=False):
    """
    Main sync function to update firmware blobs and strip unneeded files.
    """
    upstream_whence = os.path.join(upstream_dir, "WHENCE")
    local_whence = os.path.join(repo_dir, "WHENCE")

    if not os.path.exists(upstream_whence):
        sys.stderr.write(f"Error: Upstream WHENCE not found at {upstream_whence}\n")
        return 1

    baseline_paths, baseline_drivers = get_baseline_rules(local_whence)
    print(f"[*] Loaded baseline rules: {len(baseline_paths)} paths, {len(baseline_drivers)} driver sections")

    header, upstream_blocks = parse_whence_blocks(upstream_whence)

    kept_blocks_idx = set()
    synced_files = set()
    synced_links = set()
    synced_sources = set()
    synced_licenses = set()

    for idx, block in enumerate(upstream_blocks):
        files, links, sources, licenses, drivers = extract_block_entries(block)
        all_paths = files | sources | set(l[0] for l in links)

        if any(p in baseline_paths for p in all_paths) or any(d in baseline_drivers for d in drivers):
            kept_blocks_idx.add(idx)
            synced_files.update(files)
            synced_links.update(links)
            synced_sources.update(sources)
            synced_licenses.update(licenses)

    # Iterative pass: resolve link target dependencies across blocks
    changed = True
    while changed:
        changed = False
        needed_link_targets = set()
        for linkname, target in list(synced_links):
            target_rel = os.path.normpath(os.path.join(os.path.dirname(linkname), target))
            needed_link_targets.add(target_rel)

        for idx, block in enumerate(upstream_blocks):
            if idx not in kept_blocks_idx:
                files, links, sources, licenses, drivers = extract_block_entries(block)
                if any(t in files or t in sources for t in needed_link_targets):
                    kept_blocks_idx.add(idx)
                    synced_files.update(files)
                    synced_links.update(links)
                    synced_sources.update(sources)
                    synced_licenses.update(licenses)
                    changed = True

    # Also add link target paths to synced_files so the underlying target blobs are copied
    for linkname, target in list(synced_links):
        target_rel = os.path.normpath(os.path.join(os.path.dirname(linkname), target))
        if os.path.exists(os.path.join(upstream_dir, target_rel)):
            synced_files.add(target_rel)

    kept_blocks = [upstream_blocks[i] for i in sorted(list(kept_blocks_idx))]

    print(f"[*] Upstream matched {len(kept_blocks)} driver blocks out of {len(upstream_blocks)}")
    print(f"[*] Filtered firmware counts:")
    print(f"    - Files: {len(synced_files)}")
    print(f"    - Links in WHENCE: {len(synced_links)}")
    print(f"    - Sources: {len(synced_sources)}")
    print(f"    - License texts: {len(synced_licenses)}")

    if dry_run:
        print("[*] Dry-run completed. No files modified.")
        return 0

    # Reconstruct WHENCE file
    delimiter = "\n" + "-" * 74 + "\n\n"
    new_whence_content = header.rstrip() + delimiter + delimiter.join(b.strip() for b in kept_blocks) + "\n"
    with open(local_whence, "w", encoding="utf-8") as f:
        f.write(new_whence_content)

    print(f"[+] Updated WHENCE manifest")

    link_names = set(l[0] for l in synced_links)
    regular_files_to_sync = synced_files - link_names

    # Sync regular files
    copied_count = 0
    for path in regular_files_to_sync:
        src_path = os.path.join(upstream_dir, path)
        dest_path = os.path.join(repo_dir, path)

        if not os.path.exists(src_path) and not os.path.islink(src_path):
            sys.stderr.write(f"Warning: Upstream path {path} missing on disk\n")
            continue

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        if os.path.lexists(dest_path):
            os.unlink(dest_path)

        if os.path.islink(src_path):
            target = os.readlink(src_path)
            os.symlink(target, dest_path)
        else:
            shutil.copy2(src_path, dest_path, follow_symlinks=False)
        copied_count += 1

    print(f"[+] Synced {copied_count} regular firmware blobs")

    # Remove any files on disk corresponding to WHENCE Link: entries (since linux-firmware WHENCE Links are virtual and created at install time)
    for linkname, target in synced_links:
        dest_link = os.path.join(repo_dir, linkname)
        if os.path.lexists(dest_link):
            os.unlink(dest_link)

    # Sync sources (like carl9170fw or usbdux)
    for src in synced_sources:
        src_clean = src.strip().rstrip("/")
        src_path = os.path.join(upstream_dir, src_clean)
        dest_path = os.path.join(repo_dir, src_clean)

        if os.path.isdir(src_path):
            if os.path.exists(dest_path):
                shutil.rmtree(dest_path)
            shutil.copytree(src_path, dest_path, symlinks=True)
        elif os.path.isfile(src_path):
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            if os.path.lexists(dest_path):
                os.unlink(dest_path)
            shutil.copy2(src_path, dest_path, follow_symlinks=False)

    # Sync license files in LICENSES/
    lic_count = 0
    os.makedirs(os.path.join(repo_dir, "LICENSES"), exist_ok=True)
    all_actual_licenses = set()

    for lic in synced_licenses:
        src_lic = os.path.join(upstream_dir, "LICENSES", lic)
        dest_lic = os.path.join(repo_dir, "LICENSES", lic)

        if os.path.exists(src_lic):
            shutil.copy2(src_lic, dest_lic)
            all_actual_licenses.add(f"LICENSES/{lic}")
            lic_count += 1
        else:
            alt_lic = lic.replace("LICENCE", "LICENSE") if "LICENCE" in lic else lic.replace("LICENSE", "LICENCE")
            src_lic_alt = os.path.join(upstream_dir, "LICENSES", alt_lic)
            if os.path.exists(src_lic_alt):
                shutil.copy2(src_lic_alt, os.path.join(repo_dir, "LICENSES", alt_lic))
                all_actual_licenses.add(f"LICENSES/{alt_lic}")
                lic_count += 1

    print(f"[+] Synced {lic_count} license files")

    # Cleanup phase: remove any file in repo that is not in synced_files, synced_sources, license files, or tooling
    source_dir_prefixes = set(s.strip().rstrip("/") + "/" for s in synced_sources)
    stale_count = 0

    for root, dirs, files in os.walk(repo_dir):
        rel_root = os.path.relpath(root, repo_dir)
        if rel_root == ".":
            rel_root = ""

        if any(rel_root.startswith(p.rstrip("/")) for p in TOOLING_PREFIXES):
            dirs.clear()
            continue

        for fname in files:
            rel_path = os.path.join(rel_root, fname) if rel_root else fname

            if rel_path in TOOLING_FILES or any(rel_path.startswith(p) for p in TOOLING_PREFIXES):
                continue

            is_valid = (
                rel_path in synced_files
                or rel_path in synced_sources
                or rel_path in all_actual_licenses
                or any(rel_path.startswith(sp) for sp in source_dir_prefixes)
            )

            if not is_valid:
                full_path = os.path.join(repo_dir, rel_path)
                if os.path.lexists(full_path):
                    os.unlink(full_path)
                    stale_count += 1

    if stale_count > 0:
        print(f"[+] Stripped {stale_count} unneeded files")

    # Remove empty directories
    for root, dirs, files in os.walk(repo_dir, topdown=False):
        rel_root = os.path.relpath(root, repo_dir)
        if rel_root != "." and not any(rel_root.startswith(p.rstrip("/")) for p in TOOLING_PREFIXES):
            if not os.listdir(root):
                os.rmdir(root)

    # Stage changes in git so check_whence.py (which uses git ls-files) sees current disk state
    print("[*] Staging working tree changes in git...")
    subprocess.run(["git", "add", "-A"], cwd=repo_dir)

    # Validate with check_whence.py
    print("[*] Running check_whence.py validation...")
    res = subprocess.run([sys.executable, os.path.join(repo_dir, "check_whence.py")], cwd=repo_dir)
    if res.returncode != 0:
        sys.stderr.write("Error: check_whence.py failed validation!\n")
        return 1

    print("[+] Firmware update and stripping completed successfully!")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Update and strip firmware files from upstream linux-firmware git.")
    parser.add_argument("--upstream-url", default=DEFAULT_UPSTREAM_URL, help="Upstream git repository URL")
    parser.add_argument("--upstream-dir", help="Path to already cloned upstream directory (optional)")
    parser.add_argument("--branch", default="main", help="Upstream git branch to checkout")
    parser.add_argument("--dry-run", action="store_true", help="Perform parsing and check without modifying disk")
    args = parser.parse_args()

    repo_dir = os.path.abspath(os.path.dirname(__file__))

    temp_dir = None
    if args.upstream_dir:
        upstream_dir = os.path.abspath(args.upstream_dir)
    else:
        temp_dir = tempfile.mkdtemp(prefix="upstream_linux_fw_")
        print(f"[*] Cloning upstream linux-firmware ({args.upstream_url}) into temporary directory...")
        clone_cmd = ["git", "clone", "--depth", "1", "--branch", args.branch, args.upstream_url, temp_dir]
        res = subprocess.run(clone_cmd)
        if res.returncode != 0:
            sys.stderr.write("Error: Failed to clone upstream repository\n")
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
            return 1
        upstream_dir = temp_dir

    try:
        ret = sync_firmware(upstream_dir, repo_dir, dry_run=args.dry_run)
    finally:
        if temp_dir and os.path.exists(temp_dir):
            print("[*] Cleaning up temporary clone directory...")
            shutil.rmtree(temp_dir)

    sys.exit(ret)


if __name__ == "__main__":
    main()
