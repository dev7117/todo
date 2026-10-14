#!/usr/bin/env python3
"""Static rendered QA, using already-installed esbuild/Tailwind/Chromium, without listeners.

Run after frontend dependencies are available: python scripts/qa-herdr-ui.py
No downloads, backend, credentials, real tasks, persistent browser profile or sandbox bypass.
"""
import argparse
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=".local-data/herdr-ui-qa")
    parser.add_argument("--chromium", default="chromium")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    front = root / "frontend"
    output = (root / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    binary = shutil.which(args.chromium)
    if not binary:
        raise SystemExit("Chromium is not installed; nothing will be downloaded")
    tools = front / "node_modules" / ".bin"
    for name in ("esbuild", "tailwindcss"):
        if not (tools / name).exists():
            raise SystemExit(f"Existing frontend tool {name} is missing; nothing will be installed")
    env = dict(os.environ, NODE_PATH=str(front / "node_modules"))
    subprocess.run([str(tools / "esbuild"), str(root / "tests/ui/herdr-fixture.tsx"), "--bundle", "--format=iife",
                    "--jsx=automatic", "--loader:.woff2=dataurl", "--loader:.woff=dataurl", "--outfile=" + str(output / "fixture.js")],
                   check=True, cwd=front, env=env, capture_output=True)
    subprocess.run([str(tools / "tailwindcss"), "-i", str(front / "src/index.css"), "-o", str(output / "styles.css"),
                    "--config", str(front / "tailwind.config.js")], check=True, cwd=front, env=env, capture_output=True)
    page = output / "fixture.html"
    page.write_text('<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<link rel="stylesheet" href="fixture.css"><link rel="stylesheet" href="styles.css"></head>'
                    '<body><div id="root"></div><script src="fixture.js"></script></body></html>')
    cases = [("form-light-desktop", "form", "light", "blocked", 1280, 900),
             ("form-dark-desktop", "form", "dark", "blocked", 1280, 900),
             ("form-light-mobile", "form", "light", "blocked", 390, 844),
             ("form-light-mobile-scrolled", "form", "light", "blocked", 390, 844),
             ("task-blocked-light", "task", "light", "blocked", 1280, 900),
             ("task-inspection-dark", "task", "dark", "needs_inspection", 1280, 900),
             ("task-cancel-pending-mobile", "task", "light", "cancel_pending", 390, 844)]
    cases += [("remote-form-mobile", "form", "light", "blocked", 390, 844),
              ("remote-task-desktop", "task", "dark", "blocked", 1280, 900)]
    evidence = []
    for name, view, theme, state, width, height in cases:
        with tempfile.TemporaryDirectory(prefix="todo-ui-chromium-") as profile:
            command = [binary, "--headless", "--disable-background-networking", "--disable-component-update", "--disable-sync",
                       "--no-first-run", "--no-default-browser-check", "--no-proxy-server", "--hide-scrollbars",
                       "--user-data-dir=" + profile, f"--window-size={width},{height}", "--force-device-scale-factor=1",
                       "--virtual-time-budget=1800", "--dump-dom", "--screenshot=" + str(output / (name + ".png")),
                       page.as_uri() + f"?view={view}&theme={theme}&state={state}" + ("&executor=codex-app" if name.startswith("remote-") else "") + ("&scroll=bottom" if name.endswith("scrolled") else "")]
            result = subprocess.run(command, text=True, capture_output=True, timeout=30)
        match = re.search(r'data-qa="([^"]*)"', result.stdout)
        if result.returncode or not match:
            (output / (name + ".stderr.txt")).write_text(result.stderr)
            raise SystemExit(f"Rendered QA failed for {name}; inspect {output / (name + '.stderr.txt')}")
        data = json.loads(html.unescape(match.group(1)))
        data.update(case=name, viewport=[width, height], screenshot=name + ".png")
        evidence.append(data)
        if not data["pass"]:
            (output / "results.json").write_text(json.dumps(evidence, indent=2) + "\n")
            raise SystemExit(f"Rendered QA checks failed for {name}: " + json.dumps(data["checks"]))
    (output / "results.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"{len(evidence)} rendered UI scenarios passed; evidence: {output}")


if __name__ == "__main__":
    main()
