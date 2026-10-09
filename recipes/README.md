# Recipes

A recipe tells playcap which element on a page is the video player, what to
click to start it, and (optionally) which links on an index page lead to the
videos. It is a small JSON file; no Python needed. The format is described in
[`playcap/recipes.py`](../playcap/recipes.py) and
[docs/adapters.md](../docs/adapters.md#recipes).

The recipes here match **player types by their markup**, not particular
sites:

| File | Matches pages that contain |
|---|---|
| `video-js.json` | a [Video.js](https://videojs.com/) player |
| `plyr.json` | a [Plyr](https://plyr.io/) player |
| `jw-player.json` | a JW Player embedded directly in the page |
| `playcap-demo.json` | the bundled test page in `examples/demo` |

## Using one

In the playcap UI, open **Recipes**, press **Import…** and choose the file.
Recipes are stored in your `config.json`, which never leaves your machine.
The first recipe that matches a page wins; newly saved or imported ones go
first.

## Making your own

Press **Teach playcap a page**, open the page in the playcap browser, and
click the player (and the play button, and a few item links on an index
page). playcap writes the selectors for you. **Export** saves the recipe as a
file you can share.

## Contributing a recipe

Pull requests for new recipes are welcome when they:

- match a **player or page technology** (an open-source player, a common
  CMS video block, a self-hosted platform you can install yourself) by its
  markup, using `match.page_has`, rather than naming one website;
- use stable selectors (ids, `data-*` attributes, the player's own class
  names), not positions such as `:nth-of-type`;
- were tested on a page you are entitled to record.

Recipes for **paid streaming or course platforms are not accepted**, and
neither are recipes that log in, handle accounts, or get around device
limits, paywalls or protected playback. See
[RESPONSIBLE_USE.md](../RESPONSIBLE_USE.md).
