# Website (stream `website`)

**Who.** Sritej, with coding agents.

**Delivers.** The public site for LEA: what it does, how the report stays honest, early access. Static HTML and
CSS, no build step.

**Owns.** `website/**`.

**Design.** The site uses the app's design, the inspector's codex. It does not have its own. The rules are in
[DESIGN.md](../ios/RepairOS/Design/DESIGN.md) §11 (the web) and §12 (rules for generated design), and the
screens come from that Figma file. When a token changes in DESIGN.md, `website/styles.css` changes in the same PR.

**Rules.**

- **Say only what the product does today.** No made-up numbers, customers or quotes.
- **Label screens honestly.** Screens shown are design prototypes until they come from the app, and the captions
  say so.
- **No Grain.** The site never names Grain or its internals.
- **Red means danger only, and dashes mean unknown only.** As in the app.

## Tasks, in order

| Id | What | Deps |
|---|---|---|
| W1 | Home page in the codex direction (desktop, tablet, phone; paper and walnut) | done |
| W2 | Early access: a real contact address or sign-up form behind the button | W1 |
| W3 | Real screenshots from the app, replacing the Figma prototypes once the Theme.swift PR lands | W1, DESIGN.md §9 |
| W4 | Hosting and domain (a static host), with a preview per PR | W1 |
| W5 | The two or three line drawings from DESIGN.md §5, once someone draws them | W1 |
