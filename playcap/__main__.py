"""python -m playcap ui  -- open the playcap control UI."""
import sys


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("ui", "control"):
        from playcap.ui.server import main as ui_main
        return ui_main(args[1:])
    sys.exit("usage: python -m playcap ui [--port N] [--no-browser] [--root DIR]")


if __name__ == "__main__":
    main()
