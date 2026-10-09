"""python -m playcap ui        -- open the playcap control UI.
python -m playcap schedule  -- scheduled re-scan + record (see playcap.schedule)."""
import sys


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("ui", "control"):
        from playcap.ui.server import main as ui_main
        return ui_main(args[1:])
    if args[0] == "schedule":
        from playcap.schedule import main as schedule_main
        sys.exit(schedule_main(args[1:]))
    sys.exit("usage: python -m playcap ui [--port N] [--no-browser] [--root DIR]\n"
             "       python -m playcap schedule [--once|--next|--stop|--register|--unregister|--status]")


if __name__ == "__main__":
    main()
