"""Site scaffolding (ADR-004 P2) — write a starter project into a file-manager
directory for a chosen template, so it can then be deployed as a managed site.

Templates:
  static      — index.html + style.css (served directly by nginx)
  php         — index.php (served by php:8.3-apache)
  nextjs-oui  — a Next.js static-export starter pre-wired with the private
                @helios/blocks + @helios/oui design system, vendored from the
                host OUI dir. The nextjs-oui runtime builds it once in an
                ephemeral node container (npm install && next build → out/) and
                serves out/ as static — no long-running node process (ADR-004).

Pure filesystem: no Docker, no network — the build step lives in the router.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

TEMPLATES = ("static", "php", "nextjs-oui")

# Pin to the console's toolchain so the vendored OUI tarballs' peer deps resolve.
_NEXT_VERSION = "^16.2.6"
_REACT_VERSION = "^19.2.6"


_STATIC_FILES: dict[str, str] = {
    "index.html": """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>My site</title>
    <link rel="stylesheet" href="style.css" />
  </head>
  <body>
    <main>
      <h1>Hello from Aegis</h1>
      <p>A static site scaffolded by Aegis. Edit index.html and redeploy.</p>
    </main>
  </body>
</html>
""",
    "style.css": """body { font-family: system-ui, sans-serif; margin: 0; }
main { max-width: 720px; margin: 4rem auto; padding: 0 1rem; }
h1 { font-size: 2rem; }
""",
}

_PHP_FILES: dict[str, str] = {
    "index.php": """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8" /><title>My PHP site</title></head>
  <body>
    <main style="max-width:720px;margin:4rem auto;padding:0 1rem;font-family:system-ui,sans-serif">
      <h1>Hello from Aegis + PHP</h1>
      <p>Served by php:8.3-apache. Rendered at <?= date('c') ?>.</p>
    </main>
  </body>
</html>
""",
}


def _nextjs_files(blocks_tgz: str, oui_tgz: str) -> dict[str, str]:
    """Next.js static-export starter that visibly uses OUI (ThemeProvider +
    OStatusBadge — both copied from known-good console usage)."""
    package_json = {
        "name": "oui-site",
        "private": True,
        "scripts": {"dev": "next dev", "build": "next build"},
        "dependencies": {
            "next": _NEXT_VERSION,
            "react": _REACT_VERSION,
            "react-dom": _REACT_VERSION,
            "@helios/blocks": f"file:./vendor/{blocks_tgz}",
            "@helios/oui": f"file:./vendor/{oui_tgz}",
        },
        "devDependencies": {
            "typescript": "^6.0.3",
            "@types/node": "^20",
            "@types/react": "^19.0.0",
            "@types/react-dom": "^19.0.0",
        },
    }
    tsconfig = {
        "compilerOptions": {
            "target": "ES2017",
            "lib": ["dom", "dom.iterable", "esnext"],
            "allowJs": True,
            "skipLibCheck": True,
            "strict": True,
            "noEmit": True,
            "esModuleInterop": True,
            "module": "esnext",
            "moduleResolution": "bundler",
            "resolveJsonModule": True,
            "isolatedModules": True,
            "jsx": "preserve",
            "incremental": True,
            "plugins": [{"name": "next"}],
        },
        "include": ["next-env.d.ts", "**/*.ts", "**/*.tsx", ".next/types/**/*.ts"],
        "exclude": ["node_modules"],
    }
    return {
        "package.json": json.dumps(package_json, indent=2) + "\n",
        "tsconfig.json": json.dumps(tsconfig, indent=2) + "\n",
        "next.config.mjs": (
            "/** @type {import('next').NextConfig} */\nexport default { output: 'export' };\n"
        ),
        ".gitignore": "node_modules\n.next\nout\n",
        "app/layout.tsx": (
            'import "@helios/blocks/themes/professional.css";\n'
            'import "@helios/blocks/styles.css";\n'
            'import { Providers } from "./providers";\n\n'
            'export const metadata = { title: "My OUI site" };\n\n'
            "export default function RootLayout({ children }: { children: React.ReactNode }) {\n"
            "  return (\n"
            '    <html lang="en">\n'
            "      <body>\n"
            "        <Providers>{children}</Providers>\n"
            "      </body>\n"
            "    </html>\n"
            "  );\n"
            "}\n"
        ),
        "app/providers.tsx": (
            '"use client";\n'
            'import { ThemeProvider } from "@helios/blocks";\n\n'
            "export function Providers({ children }: { children: React.ReactNode }) {\n"
            '  return <ThemeProvider theme="professional">{children}</ThemeProvider>;\n'
            "}\n"
        ),
        "app/page.tsx": (
            '"use client";\n'
            'import { OStatusBadge } from "@helios/blocks";\n\n'
            "export default function Home() {\n"
            "  return (\n"
            '    <main style={{ maxWidth: 720, margin: "4rem auto", padding: "0 1rem" }}>\n'
            "      <h1>Hello from Aegis + OUI</h1>\n"
            "      <p>A Next.js static-export starter wired with the "
            "@helios/blocks design system.</p>\n"
            '      <OStatusBadge label="live" />\n'
            "    </main>\n"
            "  );\n"
            "}\n"
        ),
        "README.md": (
            "# OUI site\n\n"
            "A Next.js **static-export** starter using the `@helios/blocks` / `@helios/oui`\n"
            "design system (vendored under `vendor/`).\n\n"
            "Deploy it via Aegis with the **nextjs-oui** runtime: Aegis runs a one-shot\n"
            "`npm install && next build` in an ephemeral node container, producing `out/`,\n"
            "and serves `out/` as a static site (nginx) — no long-running node process.\n"
        ),
    }


def _vendor_oui(oui_vendor_dir: str, dest: Path) -> tuple[str, str]:
    """Copy the private OUI tarballs from *oui_vendor_dir* into ``dest/vendor``.

    Returns (blocks_tgz_name, oui_tgz_name). Raises ValueError with a clear
    message when the dir or a tarball is missing.
    """
    if not oui_vendor_dir:
        raise ValueError("nextjs-oui template needs an OUI vendor dir — set AEGIS_OUI_VENDOR_DIR")
    src = Path(oui_vendor_dir)
    if not src.is_dir():
        raise ValueError(f"oui_vendor_dir not found: {oui_vendor_dir}")

    def _pick(pattern: str) -> Path:
        matches = sorted(src.glob(pattern))
        if not matches:
            raise ValueError(f"OUI tarball {pattern} not found in {oui_vendor_dir}")
        return matches[-1]  # highest-sorting name ≈ latest version

    blocks = _pick("helios-blocks-*.tgz")
    oui = _pick("helios-oui-*.tgz")
    vendor = dest / "vendor"
    vendor.mkdir(exist_ok=True)
    shutil.copy2(blocks, vendor / blocks.name)
    shutil.copy2(oui, vendor / oui.name)
    return blocks.name, oui.name


def scaffold(template: str, dest: Path, *, oui_vendor_dir: str = "") -> list[str]:
    """Write *template* into *dest* (must be empty/absent). Returns written paths
    (relative to dest). Raises ValueError on bad template / non-empty dest /
    missing OUI tarballs."""
    if template not in TEMPLATES:
        raise ValueError(f"unknown template: {template}")
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"destination not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)

    if template == "static":
        files = dict(_STATIC_FILES)
    elif template == "php":
        files = dict(_PHP_FILES)
    else:  # nextjs-oui
        blocks_tgz, oui_tgz = _vendor_oui(oui_vendor_dir, dest)
        files = _nextjs_files(blocks_tgz, oui_tgz)

    written: list[str] = []
    for rel, content in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        written.append(rel)
    return sorted(written)
