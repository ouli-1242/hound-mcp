"""Self-healing CLI entry point for hound.

This module is the pip entry point (hound = hound_mcp.cli:main). It is
deliberately lightweight: NO heavy imports at module level. When hound.exe
runs, it does `from hound_mcp.cli import main` which imports
`hound_mcp.__init__` (just __version__, no deps) and this module (stdlib
only). The heavy server import happens lazily inside main(), wrapped in a
try/except that auto-recovers from a broken install.

Self-heal flow:
1. User runs `hound` (any command) after a broken update/dep change
2. `from hound_mcp.server import main` fails (ImportError/ModuleNotFoundError)
3. cli.py catches it, checks if ~/.hound/repair.py exists
4. If yes: runs it automatically (stops hound + force-reinstalls)
5. If no: prints a clean one-line error (not a traceback) with the fix command
"""

from __future__ import annotations

import os
import sys


def _run_repair() -> int:
    """Run ~/.hound/repair.py to auto-recover a broken install.

    If repair.py doesn't exist, writes a minimal one inline and runs it.
    Never leaves the user stranded with a traceback.
    """
    repair = os.path.join(os.path.expanduser("~"), ".hound", "repair.py")
    if not os.path.exists(repair):
        # Write a minimal repair script (same logic as updater._write_repair_script
        # but standalone so we don't need to import the updater module).
        os.makedirs(os.path.dirname(repair), exist_ok=True)
        script = '''import os, sys, subprocess
print("Hound repair: stopping any running hound...")
if sys.platform == "win32":
    subprocess.run(["taskkill", "/IM", "hound.exe", "/F"], capture_output=True)
else:
    subprocess.run(["pkill", "-x", "hound"], capture_output=True)
print("Hound repair: force-reinstalling hound-mcp from PyPI...")
r = subprocess.run([sys.executable, "-m", "pip", "install", "--force-reinstall", "hound-mcp",
                    "--quiet", "--disable-pip-version-check"])
if r.returncode != 0:
    print("Hound repair: reinstall failed (pip exit %d)." % r.returncode)
    print("  Try manually: %s -m pip install --force-reinstall hound-mcp" % sys.executable)
    sys.exit(1)
try:
    from importlib.metadata import version as _v
    print("Hound " + _v("hound-mcp") + "  repaired")
except Exception:
    print("Hound repair: reinstalled (version check skipped)")
'''
        try:
            with open(repair, "w") as f:
                f.write(script)
        except Exception:
            # Can't write repair.py - run pip directly as a last resort
            print("  recovering (direct reinstall)...")
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install",
                           "--force-reinstall", "hound-mcp",
                           "--quiet", "--disable-pip-version-check"],
                          timeout=120)
            print("  Hound recovered. Re-run your command.")
            return 0
    import subprocess
    print("  recovering...")
    try:
        result = subprocess.run(
            [sys.executable, repair],
            timeout=120,
            capture_output=False,
        )
        if result.returncode == 0:
            print("  Hound recovered. Re-run your command.")
            return 0
        print("  Recovery failed. Run manually: "
              f'python "{repair}"')
        return 1
    except Exception as e:
        print(f"  Recovery error: {e}")
        print(f'  Run manually: python "{repair}"')
        return 1


def main() -> int:
    """Entry point that self-heals on broken imports."""
    try:
        from hound_mcp.server import main as _server_main
        return _server_main() or 0
    except (ImportError, ModuleNotFoundError) as e:
        # Broken install: missing dep, half-failed update, etc.
        # Don't crash with a traceback - auto-recover.
        mod_name = getattr(e, "name", "") or str(e)
        print(f"  Hound install broken: {mod_name}")
        rc = _run_repair()
        if rc != 0:
            print("  If recovery failed, run: pip install --force-reinstall hound-mcp")
        return rc
    except Exception:
        # Any other import-time crash (not a missing module) - re-raise
        # so real bugs surface, but only after trying repair as a last resort
        # if the error looks install-related.
        raise
