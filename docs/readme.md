# Documentation

This directory contains the VitePress site for adorm, published on docs.velis.si.

## Development

```bash
npm install
npm run docs:dev
```

The site is served at <http://localhost:5173/>.

## Building

```bash
npm run docs:build
```

The built site is in `.vitepress/dist`. The deployment passes `--base` for the site's path on docs.velis.si;
links to other projects' sites use `vitepress-plugin-crosslinks` (`[text](:muxws:/path)`), and their base
URLs can be overridden with `CROSSLINKS_BASE_<NAME>` environment variables.

## Structure

- `.vitepress/config.ts`: title, navigation, sidebar, crosslink targets
- `index.md`: the landing page
- `intro/`: what adorm is, changelog
- `topics/`: topic guides
- `howto/`: how-to guides
- `ref/`: API reference

## Adding a page

1. Create the Markdown file in the matching section directory.
2. Add `{ text, link }` for it to the sidebar in `.vitepress/config.ts`; `link` is the path without `.md`.

`tests/test_docs.py` fails when a sidebar, navigation or landing-page link has no page, when a page is not
linked from the sidebar or navigation, or when a name in `adorm.__all__` has no reference heading
(`` ### `adorm.name` ``) in `ref/`.
