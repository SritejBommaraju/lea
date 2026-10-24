# website

The LEA home page: static HTML and CSS, no build step and no JavaScript. Stream and tasks:
[areas/website.md](../areas/website.md). The design rules are in
[DESIGN.md §11-§12](../ios/RepairOS/Design/DESIGN.md) (PR #15): the inspector's codex, applied to the web.

```
python3 -m http.server 8080 --directory website    # then open http://localhost:8080
```

| Path | What |
|---|---|
| `index.html` | Opening with the report, method (three steps), the report and its rules, at the bench, what photos can't tell, early access |
| `styles.css` | Paper and walnut tokens from DESIGN.md §3, light and dark by `prefers-color-scheme`, breakpoints at 1080 and 760 px |
| `img/` | Screens exported from the app's Figma file (PR #15), light and dark. Design prototypes, and captioned as such |

Fonts: EB Garamond for display and margin notes, from Google Fonts (SIL OFL 1.1, the same licence question as
DESIGN.md §10). Body text is the system font (SF Pro on Apple devices). Labels are SF Mono, with JetBrains Mono
as the fallback.

The early-access buttons scroll to the closing section for now (W2).
