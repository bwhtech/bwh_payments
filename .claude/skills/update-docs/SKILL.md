---
name: update-docs
description: Write or update the BWH Payments developer docs on docs.bwh.tech (a Frappe Wiki site), including animated SVG figures. Use this whenever a change to bwh_payments alters behaviour, the public API, the gateway contract or setup steps, when someone asks to draft, sync, review or publish the docs, compare docs to code, or add a diagram, figure or animation to a docs page, even if they only say "update the docs" or name one page.
---

# Update the BWH Payments docs

The docs live on a Frappe Wiki site, not in this repo. Pages are Markdown, and every edit goes through a
**change request**: create one, apply operations, then submit, approve and merge it. Figures are standalone
animated SVG files, uploaded to the site and embedded as Markdown images.

## Where things are

| What | Value |
|---|---|
| Site | `https://docs.bwh.tech`, `frappectl` profile `bwh_docs` (writable) |
| Published space | `BWH Payments`, route `bwh-payments`, Wiki Space `cpjar1gqo9` |
| Next space (published 2026-10-11) | `BWH Payments (Next)`, route `bwh-payments-next`, Wiki Space `f55b4un2at` |
| Change request API | `wiki.frappe_wiki.doctype.wiki_change_request.wiki_change_request` |
| Wiki app source | `apps/wiki` in the bench (read it when an API call surprises you) |
| Figure generator | `scripts/figures.py` in this skill |

The Next space documents the current code (services refactor and later). It was published on 2026-10-11, so
guests can read it, and both spaces are live. Write there unless the user says otherwise.

Page tree in both spaces:

```text
get-started/   overview, accept-a-payment, return-pages
gateways/      stripe, razorpay, telr, tabby
build/         build-a-payment-gateway, service-clients, test-with-stubs, webhooks-and-security
reference/     gateway-contract, public-api
```

The published space has only overview, return-pages, the gateway pages, build-a-payment-gateway,
gateway-reference and webhooks-and-security until the draft is promoted (see step 6).

## Ground rules

- Writes to the site need the user's go-ahead for that space. Reads are free. The draft space counts as
  approved once the user has asked for drafts there.
- Never publish to `bwh-payments` without an explicit ask.
- Never write `Wiki Document` rows directly. Go through a change request, or the wiki's revision history
  goes out of sync.
- Every claim on a page must match the code on the branch you document. Check field names, signatures and
  defaults in the source before you write them. Example: the Gateway Payment Request has
  `customer_forenames` and `customer_surname`, not `customer_name`.
- Write in Simplified Technical English (the `technical-writing` skill), in the voice of the existing pages.
- Prefix `frappectl` with `PYTHONWARNINGS=ignore`, and send stderr to `/dev/null` when you parse JSON.

## 1. Find what changed

```bash
git log --oneline upstream/develop..HEAD
git diff --stat upstream/develop...HEAD
```

Map each change to the pages it touches. From the services refactor:

| Code | Pages |
|---|---|
| `bwh_payments/base_class.py` (contract, models) | reference/gateway-contract, build/build-a-payment-gateway |
| `bwh_payments/api.py` | reference/public-api, get-started/accept-a-payment, get-started/overview |
| `bwh_payments/services/<name>/` | build/service-clients, build/test-with-stubs, the gateway's page |
| `bwh_payments/bwh_payments/webhook.py` | build/webhooks-and-security |
| A gateway controller | gateways/<name> |
| `pyproject.toml` dependency floors | get-started/overview, gateways/stripe |

## 2. Read the current pages

Download each page's Markdown to a scratch folder and edit it there:

```bash
F="frappectl -s bwh_docs"
$F doc list "Wiki Document" -f wiki_space=f55b4un2at --fields name,slug,route --limit 50 --json 2>/dev/null
$F doc get "Wiki Document" <name> --json 2>/dev/null \
  | python3 -c "import json,sys;d=json.load(sys.stdin);open(d['slug']+'.md','w').write(d['content'])"
```

Wiki Markdown supports tables, fenced code, ```` ```mermaid ```` blocks and callouts:

```text
:::note[Optional title]    :::tip    :::caution[...]    :::danger[...]
body
:::
```

Internal links use the space route: `/bwh-payments-next/build/service-clients`. When you copy a page
between spaces, rewrite the route prefix.

## 3. Push pages through a change request

The draft request is per user and per space, and it is reused until it merges.

```bash
M=wiki.frappe_wiki.doctype.wiki_change_request.wiki_change_request
F="frappectl -s bwh_docs"
CR=$($F api -X POST method/$M.get_or_create_draft_change_request \
  -f wiki_space=f55b4un2at -f "title=<what changed>" 2>/dev/null \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['name'])")
$F api method/$M.get_cr_tree -f name=$CR --method GET 2>/dev/null   # root_group and each page's doc_key
```

Build the operations as JSON with Python (never shell-quote Markdown) and apply them in one batch. The
batch is atomic.

```python
ops = [
	# a new group and a page under it; temp keys resolve within the batch
	{"type": "create_node", "temp_key": "g1", "parent_key": "<root_group or a doc_key>",
	 "title": "Reference", "slug": "reference", "is_group": 1, "is_published": 1, "order_index": 3},
	{"type": "create_node", "temp_key": "p1", "parent_key": "g1", "title": "Public API",
	 "slug": "public-api", "is_group": 0, "is_published": 1, "order_index": 0, "content": md},
	# an existing page
	{"type": "update_content", "doc_key": "<doc_key>", "content": md},
]
json.dump({"name": CR, "operations": ops}, open("ops.json", "w"))
```

```bash
$F api -X POST method/$M.apply_cr_operations --input ops.json 2>/dev/null
for m in submit_change_request approve_change_request merge_change_request; do
  $F api -X POST method/$M.$m -f name=$CR 2>/dev/null
done
```

- Writes need `-X POST`. Without it, `frappectl` sends a GET and Frappe rolls the write back.
- `merge_change_request` refuses a Draft. Submit and approve it first.
- The user has approved self-approval: submit, approve and merge your own change requests in one go. A
  request left in review hides its changes, and that looks like a broken page (figures "missing").
  Before you report, list `Wiki Change Request` for the space and check none of yours is still open.
- The reader 404s every page of an unpublished space, even for signed-in users (`check_published`), and
  `get_page_data` does too. Check draft pages in the editor, or render the stored Markdown with
  `wiki.wiki.markdown.render_markdown` from the bench env.
- Other operation types: `update_node` (title, slug, published), `move_node`, `reorder_children`,
  `delete_node`.
- To confirm the merge, list the space's `Wiki Document` rows. A guest `curl` of a draft-space page returns
  404 on purpose.

## 4. Figures

Use a figure only where motion explains something a table or Mermaid block can't. Two styles, chosen with
the user (comparison: https://claude.ai/artifact/2cc8iKDMwbhGKeUtkhHAVx):

| Style | Use for | Builder |
|---|---|---|
| **A: Hairline** | Structure: parts, how they connect, the two paths a status can take | `Fig` + `node`, `travel`, `appear`, `vanish` |
| **D: Sequence** | Order: who calls whom, step by step | `sequence(fig_no, caption, title, lanes, items, status)` |

Existing figures and where they appear. Each one is two files on the site, `<name>-light.svg` and
`<name>-dark.svg`. The older single files (`/files/bwhp-*.svg` with no suffix) follow the OS theme and are
no longer used.

| File | Style | Pages |
|---|---|---|
| `bwhp-settle-flow.svg` | A | get-started/overview |
| `bwhp-service-clients.svg` | A | build/service-clients, build/build-a-payment-gateway |
| `bwhp-gateway-contract.svg` | A | reference/gateway-contract |
| `bwhp-test-with-stubs.svg` | D | build/test-with-stubs |
| `bwhp-tabby-capture.svg` | D | gateways/tabby (replaces its Mermaid block) |

Rules for every figure. `figures.py` already follows them, so keep to its helpers:

- **Standalone SVG shown through `<img>`.** No script, no external fonts, no links. A wiki image can't run
  JavaScript, so the motion is CSS only.
- **The still frame is the final frame.** Elements rest at their end state, and animations only move them
  away from it. `prefers-reduced-motion` turns animation off and leaves a complete diagram.
- **One loop, 6.5 s,** with timings in percent of the loop. Keep steps in story order.
- **One file per theme.** `figures.py` writes `<name>-light.svg` and `<name>-dark.svg` from `THEMES`. Never
  put a `prefers-color-scheme` query inside the SVG: it follows the OS, so a reader with a dark OS and the
  wiki in light mode sees pale strokes on white. The wiki points a `<picture>`'s dark `<source>` at its own
  theme toggle (`syncPictureSources` in the wiki's `layout.html`).
- **A `<title>`** that says what happens, in one or two sentences. Reuse it as the image's alt text.
- **Real names only:** methods, fields and statuses from the code, never placeholders.
- **Number figures** in the header (`FIG. n`) in reading order through the space.

Make, check, upload, embed:

```bash
cd .claude/skills/update-docs/scripts
OUT=<scratch>/figs; mkdir -p $OUT
python3 -I figures.py $OUT --still                    # or name one: figures.py $OUT bwhp-tabby-capture --still
qlmanage -t -s 900 -o $OUT/still $OUT/still/*.svg     # Quick Look renders only frame 0, so check the still copies
```

The still copies sit on the page colour of their theme. Read both PNGs of each figure and fix overlaps,
text that runs past a box, and clipped labels. Then upload both files and confirm the site serves each one
byte for byte:

```bash
for f in $OUT/<name>-light.svg $OUT/<name>-dark.svg; do
  frappectl -s bwh_docs file upload $f 2>/dev/null                 # note the file_url it returns
  curl -s https://docs.bwh.tech/files/$(basename $f) | cmp - $f && echo same
done
```

If the file name is already taken by different content, Frappe saves under a new name. Always use the
`file_url` from the upload result. Embed the pair on the page in exactly this shape, because the wiki's
`PICTURE_BLOCK_PATTERN` only accepts it. Escape `"` in the alt text:

```html
<picture>
  <source srcset="/files/<name>-dark.svg" media="(prefers-color-scheme: dark)">
  <img src="/files/<name>-light.svg" alt="<the figure's title text>">
</picture>
```

While a figure isn't drawn yet, mark its place with a callout, so it's easy to find:

```text
:::note[Figure pending]
Animated: <what the figure shows>.
:::
```

## 5. Report

Tell the user which pages changed, the change request names, and any claim you couldn't check against the
code. Only signed-in users can open the draft space.

## 6. Promote the draft (after the PR merges)

1. Confirm the code PR is merged into `develop`.
2. Open a change request on `cpjar1gqo9`.
3. Recreate the draft tree there. Rewrite `/bwh-payments-next/` links to `/bwh-payments/`, and rename
   `build/gateway-reference` to `reference/gateway-contract`, or keep the old slug and update the links.
4. Delete published pages the draft replaced. Ask the user first, because old links break.
5. Merge only after the user approves.
