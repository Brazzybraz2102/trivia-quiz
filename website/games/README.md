# Fall Fest Games page for Braznarkel.com

A single page at `braznarkel.com/games` that shows off the ten Fall Fest games, offers the download, and explains how to host.

## Files

- `index.html`: the page.
- `logo.svg`: the Braznarkel Ranch wordmark.
- `braznarkel-ranch-fall-fest-2026.zip`: the game download. It is built from the `fallfest/` folder and is not stored in git. Rebuild it from the repository root with:

  ```sh
  git archive --format=zip --prefix=braznarkel-ranch-fall-fest/ -o website/games/braznarkel-ranch-fall-fest-2026.zip HEAD:fallfest
  ```

## Adding it to the Netlify site

Put this `games` folder inside the folder Netlify publishes for Braznarkel.com, next to the site's home page, then deploy as usual. The page is then live at `https://braznarkel.com/games/`.

- **Site deploys from Git:** commit the `games` folder into the publish directory and push.
- **Site deploys by drag and drop:** drag the whole site folder, with `games` inside it, onto the Deploys page. Dragging only the `games` folder replaces the entire site.

To link to it from the home page, add a button like:

```html
<a href="/games/">Play the Fall Fest games</a>
```
