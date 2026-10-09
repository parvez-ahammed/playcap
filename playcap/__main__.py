"""python -m playcap ui  -- open the playcap control UI.
   playcap schedule     -- scheduled re-scan + record (see playcap.schedule).
   playcap --version    -- print the installed version.

The version comes from the installed distribution's metadata, so it matches
what pip/uvx installed; a source checkout that was never installed falls back
to playcap.__version__.
"""
import sys

USAGE = ("usage: python -m playcap ui [--port N] [--no-browser] [--root DIR] | --version\n"
         "       python -m playcap schedule [--once|--next|--stop|--register|--unregister|--status]")


def version():
    try:
        from importlib.metadata import PackageNotFoundError, version as dist_version
        try:
            return dist_version("playcap")
        except PackageNotFoundError:
            pass
    except ImportError:
        pass
    from playcap import __version__
    return __version__


def main():
    args = sys.argv[1:]
    if args and args[0] in ("--version", "-V", "version"):
        print(f"playcap {version()}")
        return
    if not args or args[0] in ("ui", "control"):
        from playcap.ui.server import main as ui_main
        return ui_main(args[1:])
    if args[0] == "schedule":
        from playcap.schedule import main as schedule_main
        sys.exit(schedule_main(args[1:]))
    sys.exit(USAGE)


if __name__ == "__main__":
    main()
