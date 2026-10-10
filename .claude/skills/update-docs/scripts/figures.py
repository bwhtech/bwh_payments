"""Animated SVG figures for the BWH Payments docs: style A (hairline) for structure, style D (sequence) for
ordered flows. Add a figure by writing a function that returns `Fig(...).svg()` or `sequence(...)`, then
register it in FIGURES.

Every figure is a standalone SVG for <img>: CSS animation only, a 6.5 s loop, and its un-animated state is
the final frame, so prefers-reduced-motion shows a complete still. Each figure is written twice, NAME-light.svg
and NAME-dark.svg, and embedded with <picture>. The wiki points the dark <source> at its own theme toggle;
a prefers-color-scheme query inside the SVG would follow the OS instead.
"""

import sys
from html import escape
from pathlib import Path

LOOP = 6.5
THEMES = {
	"light": "--ink:#1d1d1f;--mute:#7d7f85;--line:#c9cacd;--acc:#2f5bea;--ok:#1a7f4b;--okbg:#e6f4ec;--note:#f5f6f7",
	"dark": "--ink:#ececee;--mute:#90929a;--line:#45474d;--acc:#86a2ff;--ok:#5fd394;--okbg:#173626;--note:#1f2125",
}

BASE_CSS = (
	"""
:root{/*tokens*/}
text{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;fill:var(--ink)}
.lbl{font-size:10.5px;letter-spacing:.07em;fill:var(--mute)}
.ttl{font-size:12px}
.sm{font-size:11px}
.mute{fill:var(--mute)}
.box{fill:none;stroke:var(--ink);stroke-width:1}
.g{fill:none;stroke:var(--ink);stroke-width:1;stroke-linecap:round;stroke-linejoin:round}
.wire{fill:none;stroke:var(--line);stroke-width:1;stroke-dasharray:2 3}
.hot{fill:none;stroke:var(--acc);stroke-width:1.25}
.dot{fill:var(--acc)}
.pill{fill:var(--note);stroke:var(--line);stroke-width:1}
.pillok{fill:var(--okbg);stroke:var(--ok);stroke-width:1}
.oktext{fill:var(--ok)}
.okdot{fill:var(--ok)}
.okstroke{fill:none;stroke:var(--ok);stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.a{animation-duration:%ss;animation-iteration-count:infinite;animation-timing-function:ease-in-out;animation-fill-mode:both}
@media (prefers-reduced-motion:reduce){.a{animation:none!important}}
"""
	% LOOP
)


class Fig:
	def __init__(self, w, h, title):
		self.w, self.h, self.title = w, h, title
		self.body, self.css, self.n = [], [], 0

	def add(self, s):
		self.body.append(s)

	def _name(self):
		self.n += 1
		return f"k{self.n}"

	def appear(self, inner, at, until=None, rest=1):
		"""Fade in at `at`%; fade out at `until`% (or at the loop's end). `rest` is the still-frame opacity."""
		n = self._name()
		if until is None:
			frames = f"0%,{at}%{{opacity:0}}{at + 3}%,93%{{opacity:1}}100%{{opacity:0}}"
		else:
			frames = f"0%,{at}%{{opacity:0}}{at + 3}%,{until}%{{opacity:1}}{until + 3}%,100%{{opacity:0}}"
		self.css.append(f"@keyframes {n}{{{frames}}}")
		self.add(f'<g class="a" style="animation-name:{n};opacity:{rest}">{inner}</g>')

	def vanish(self, inner, at, rest=0):
		"""Visible from the start, fades out at `at`%, returns at the loop's end."""
		n = self._name()
		self.css.append(f"@keyframes {n}{{0%,{at}%{{opacity:1}}{at + 3}%,94%{{opacity:0}}100%{{opacity:1}}}}")
		self.add(f'<g class="a" style="animation-name:{n};opacity:{rest}">{inner}</g>')

	def travel(self, x1, y1, x2, y2, start, end, r=3.5):
		"""A dot that runs from (x1,y1) to (x2,y2) between `start`% and `end`%. Hidden in the still frame."""
		n = self._name()
		dx, dy = x2 - x1, y2 - y1
		self.css.append(
			f"@keyframes {n}{{0%,{start}%{{transform:translate(0,0);opacity:0}}{start + 1}%{{opacity:1}}"
			f"{end}%{{transform:translate({dx}px,{dy}px);opacity:1}}{end + 2}%,100%{{transform:translate({dx}px,{dy}px);opacity:0}}}}"
		)
		self.add(f'<circle class="dot a" style="animation-name:{n};opacity:0" cx="{x1}" cy="{y1}" r="{r}"/>')

	def svg(self):
		return (
			f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" '
			f'height="{self.h}" role="img" aria-labelledby="t">\n<title id="t">{escape(self.title)}</title>\n'
			f"<style>{BASE_CSS}{''.join(self.css)}</style>\n" + "\n".join(self.body) + "\n</svg>\n"
		)


def t(x, y, s, cls="ttl", anchor="start"):
	return f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{escape(s)}</text>'


def header(f, fig_no, caption):
	f.add(t(16, 26, f"FIG. {fig_no}   {caption.upper()}", "lbl"))
	f.add(f'<line x1="16" x2="{f.w - 16}" y1="38" y2="38" stroke="var(--line)" stroke-width="1"/>')


def pill(x, y, w, label, ok=False):
	cls, tcls = ("pillok", "lbl oktext") if ok else ("pill", "lbl")
	return f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="18" rx="9"/>' + t(
		x + w / 2, y + 13, label, tcls, "middle"
	)


# --- glyphs, drawn around a centre point -----------------------------------------------------------
def g_card(cx, cy):
	return (
		f'<g class="g"><rect x="{cx - 20}" y="{cy - 13}" width="40" height="26" rx="4"/>'
		f'<line x1="{cx - 20}" x2="{cx + 20}" y1="{cy - 5}" y2="{cy - 5}"/><line x1="{cx - 13}" x2="{cx - 2}" y1="{cy + 6}" y2="{cy + 6}"/></g>'
	)


def g_shield(cx, cy):
	return (
		f'<path class="g" d="M{cx} {cy - 18} L{cx + 16} {cy - 12} V{cy + 2} C{cx + 16} {cy + 12} {cx + 8} {cy + 17} {cx} {cy + 20} '
		f'C{cx - 8} {cy + 17} {cx - 16} {cy + 12} {cx - 16} {cy + 2} V{cy - 12} Z"/>'
	)


def g_doc(cx, cy):
	return (
		f'<g class="g"><path d="M{cx - 14} {cy - 20} h19 l9 9 v31 h-28 z"/>'
		f'<line x1="{cx - 8}" x2="{cx + 8}" y1="{cy - 3}" y2="{cy - 3}"/><line x1="{cx - 8}" x2="{cx + 4}" y1="{cy + 4}" y2="{cy + 4}"/></g>'
	)


def g_box(cx, cy):
	return (
		f'<g class="g"><path d="M{cx - 17} {cy - 8} l17 -9 l17 9 v19 l-17 9 l-17 -9 z"/>'
		f'<path d="M{cx - 17} {cy - 8} l17 9 l17 -9 M{cx} {cy + 1} v19"/></g>'
	)


def g_person(cx, cy):
	return f'<g class="g"><circle cx="{cx}" cy="{cy - 9}" r="7"/><path d="M{cx - 14} {cy + 17} c0 -10 6 -15 14 -15 s14 5 14 15"/></g>'


def g_cloud(cx, cy):
	return f'<path class="g" d="M{cx - 18} {cy + 10} h34 a9 9 0 0 0 0 -18 a12 12 0 0 0 -23 -3 a9 9 0 0 0 -11 21 z"/>'


def g_chip(cx, cy):
	return (
		f'<g class="g"><rect x="{cx - 14}" y="{cy - 14}" width="28" height="28" rx="3"/>'
		+ "".join(
			f'<line x1="{cx - 8 + i * 8}" x2="{cx - 8 + i * 8}" y1="{cy - 19}" y2="{cy - 14}"/><line x1="{cx - 8 + i * 8}" x2="{cx - 8 + i * 8}" y1="{cy + 14}" y2="{cy + 19}"/>'
			for i in range(3)
		)
		+ "</g>"
	)


def check(cx, cy):
	return f'<path class="okstroke" d="M{cx - 7} {cy + 1} l5 5 l9 -10"/>'


def node(f, x, y, w, h, glyph, title, sub):
	f.add(f'<rect class="box" x="{x}" y="{y}" width="{w}" height="{h}" rx="10"/>')
	f.add(glyph(x + w / 2, y + h / 2 - 6))
	f.add(t(x, y + h + 22, title))
	if sub:
		f.add(t(x, y + h + 38, sub, "lbl"))


# --- A1: overview — two ways a payment settles ----------------------------------------------------
def overview():
	f = Fig(
		722,
		250,
		"The shopper pays at the gateway. A signed webhook marks the payment request Paid and the order "
		"is notified. A later status check finds it already Paid and changes nothing.",
	)
	header(f, 1, "Two ways a payment settles")
	Y, W, H = 76, 120, 96
	X = [16, 186, 416, 586]
	node(f, X[0], Y, W, H, g_person, "Shopper", "pays on hosted page")
	node(f, X[1], Y, W, H, g_card, "Gateway", "checkout session")
	node(f, X[2], Y, W, H, g_doc, "Payment Request", "order_ref")
	node(f, X[3], Y, W, H, g_box, "Your order", "ref_doc")
	mid = Y + H / 2
	top, bot = Y + 22, Y + H - 22
	f.add(f'<line class="wire" x1="{X[0] + W}" x2="{X[1]}" y1="{mid}" y2="{mid}"/>')
	f.add(f'<line class="wire" x1="{X[2] + W}" x2="{X[3]}" y1="{mid}" y2="{mid}"/>')
	f.add(f'<line class="wire" x1="{X[1] + W}" x2="{X[2]}" y1="{top}" y2="{top}"/>')
	f.add(f'<line class="wire" x1="{X[1] + W}" x2="{X[2]}" y1="{bot}" y2="{bot}"/>')
	f.add(t((X[1] + W + X[2]) / 2, top - 7, "WEBHOOK", "lbl", "middle"))
	f.add(t((X[1] + W + X[2]) / 2, bot + 15, "SYNC_STATUS()", "lbl", "middle"))
	f.travel(X[0] + W, mid, X[1], mid, 3, 14)
	f.appear(check(X[1] + W - 16, Y + 16), 15)
	f.travel(X[1] + W, top, X[2], top, 20, 33)
	px = X[2] + W / 2 - 32
	f.vanish(pill(px, Y + H - 28, 64, "PENDING"), 35)
	f.appear(pill(px, Y + H - 28, 64, "PAID", ok=True), 35)
	f.travel(X[2] + W, mid, X[3], mid, 42, 52)
	f.appear('<circle class="okdot" cx="{}" cy="{}" r="4"/>'.format(X[3] + W - 14, Y + 14), 53)
	f.appear(t(X[3] + W, Y + H + 56, "on_payment_authorized()", "lbl oktext", "end"), 53)
	f.travel(X[1] + W, bot, X[2], bot, 62, 74)
	f.appear(t(X[2], Y - 8, "already Paid: ignored", "lbl"), 75)
	return f.svg()


# --- A2: service clients --------------------------------------------------------------------------
def service_clients():
	f = Fig(
		722,
		300,
		"The settings controller calls a client. On a site, frappe.in_test is false and the live client "
		"calls the gateway's API. In tests it is true and the stub answers from memory.",
	)
	header(f, 2, "One controller, two clients")
	cy, ly, sy = 166, 104, 228  # controller centre, live row, stub row
	f.add(f'<rect class="box" x="16" y="{cy - 50}" width="140" height="100" rx="10"/>')
	f.add(g_chip(86, cy - 10))
	f.add(t(16, cy + 72, "Settings controller"))
	f.add(t(16, cy + 88, "every decision", "lbl"))
	fx = 220
	f.add(f'<line class="wire" x1="156" x2="{fx}" y1="{cy}" y2="{cy}"/>')
	f.add(f'<circle cx="{fx}" cy="{cy}" r="5" fill="none" stroke="var(--ink)"/>')
	f.add(t(fx + 12, cy + 4, "frappe.in_test", "sm"))
	bx, bw = 330, 176
	for y, title, sub in [(ly, "live.py", "HTTP / SDK + logs"), (sy, "stub.py", "state in memory")]:
		f.add(f'<path class="wire" d="M{fx} {cy + (-5 if y < cy else 5)} V{y} H{bx}"/>')
		f.add(f'<rect class="box" x="{bx}" y="{y - 30}" width="{bw}" height="60" rx="10"/>')
		f.add(t(bx + 14, y - 4, title))
		f.add(t(bx + 14, y + 14, sub, "lbl"))
	ax = 576
	f.add(f'<line class="wire" x1="{bx + bw}" x2="{ax}" y1="{ly}" y2="{ly}"/>')
	f.add(f'<rect class="box" x="{ax}" y="{ly - 30}" width="130" height="60" rx="10"/>')
	f.add(g_cloud(ax + 30, ly))
	f.add(t(ax + 62, ly - 2, "Gateway", "sm"))
	f.add(t(ax + 62, ly + 14, "real API", "lbl"))
	f.add(f'<rect class="pill" x="{ax}" y="{sy - 18}" width="130" height="36" rx="8"/>')
	f.add(t(ax + 65, sy + 4, "no network", "lbl", "middle"))
	# phase 1: on a site
	f.appear(t(f.w - 16, 58, "ON A SITE", "lbl", "end"), 2, 44, rest=0)
	f.appear(t(fx + 12, cy + 20, "False", "sm oktext"), 2, 44, rest=0)
	f.appear(f'<path class="hot" d="M156 {cy} H{fx} V{ly} H{bx} M{bx + bw} {ly} H{ax}"/>', 3, 44, rest=0)
	f.travel(156, cy, fx, cy, 6, 12)
	f.travel(fx, cy, fx, ly, 12, 18)
	f.travel(fx, ly, bx, ly, 18, 24)
	f.travel(bx + bw, ly, ax, ly, 26, 34)
	f.appear(check(ax + 114, ly - 16), 35, 44, rest=0)
	# phase 2: in tests (this is the still frame)
	f.appear(t(f.w - 16, 58, "IN TESTS", "lbl", "end"), 50)
	f.appear(t(fx + 12, cy + 20, "True", "sm oktext"), 50)
	f.appear(f'<path class="hot" d="M156 {cy} H{fx} V{sy} H{bx}"/>', 51)
	f.travel(156, cy, fx, cy, 54, 60)
	f.travel(fx, cy, fx, sy, 60, 66)
	f.travel(fx, sy, bx, sy, 66, 72)
	f.appear(check(bx + bw - 16, sy - 16), 74)
	return f.svg()


# --- A3: gateway contract -------------------------------------------------------------------------
def contract():
	rows = [
		("create_session()", "CheckoutSession", False),
		("get_payment_status()", "str", False),
		("refund_payment()", "RefundResult", False),
		("handle_webhook()", "WebhookEvent | None", False),
		("cancel_session()", "bool · optional", True),
	]
	f = Fig(
		722,
		356,
		"BWH Payments calls five methods on a gateway's settings controller. Each returns a model: "
		"CheckoutSession, a status string, RefundResult, WebhookEvent or None, and a bool for the optional cancel_session.",
	)
	header(f, 3, "The gateway contract")
	top, gap = 84, 46
	bh = gap * (len(rows) - 1) + 60
	f.add(f'<rect class="box" x="16" y="{top - 30}" width="150" height="{bh}" rx="10"/>')
	f.add(t(30, top - 8, "BWH Payments", "sm"))
	for i, line in enumerate(["Gateway Payment", "Request", "", "webhook.handle", "", "refund ledger"]):
		f.add(t(30, top + 14 + i * 15, line, "lbl"))
	f.add(f'<rect class="box" x="556" y="{top - 30}" width="150" height="{bh}" rx="10"/>')
	f.add(t(570, top - 8, "Your gateway", "sm"))
	f.add(t(570, top + 8, "Document +", "lbl"))
	f.add(t(570, top + 22, "PaymentGatewayBase", "lbl"))
	f.add(
		t(
			16,
			top + bh + 6,
			"Results are read with <Model>.model_validate, so a plain dict still works.",
			"lbl",
		)
	)
	step = 80 / len(rows)
	for i, (method, model, optional) in enumerate(rows):
		y = top + 14 + i * gap
		dash = ' stroke-dasharray="5 4"' if optional else ""
		f.add(f'<line x1="166" x2="556" y1="{y}" y2="{y}" stroke="var(--line)"{dash}/>')
		f.add(t(178, y - 7, method, "sm"))
		s = 4 + i * step
		f.travel(166, y, 556, y, round(s), round(s + step * 0.45))
		f.travel(556, y, 166, y, round(s + step * 0.5), round(s + step * 0.95))
		f.appear(t(544, y + 15, f"← {model}", "lbl oktext", "end"), round(s + step * 0.95))
	return f.svg()


# --- D: sequences ---------------------------------------------------------------------------------
SEQ_CSS = """
.head{font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;font-size:12.5px;font-weight:600}
.code{font-size:11px}
.life{stroke:var(--line);stroke-width:1;stroke-dasharray:4 4}
.hd{fill:var(--note);stroke:var(--line)}
.msg{stroke:var(--acc);stroke-width:1.5;fill:none;marker-end:url(#ah)}
.ret{stroke:var(--mute);stroke-width:1.25;stroke-dasharray:5 4;fill:none;marker-end:url(#ahr)}
.n{fill:var(--note);stroke:var(--line)}
.step{font-size:10px;font-weight:700;fill:var(--acc)}
"""


def sequence(fig_no, caption, title, lanes, items, status=None, w=722):
	"""items: ("msg"|"ret", from_lane, to_lane, label) or ("note", lane, label).
	status: (lane, after_item, before, after) shows a pill on that lane that flips after that item."""
	row = 42
	h = 96 + row * len(items) + 24
	f = Fig(w, h, title)
	f.css.append(SEQ_CSS)
	header(f, fig_no, caption)
	f.add(
		'<defs><marker id="ah" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">'
		'<path d="M0 0 L8 4 L0 8 z" fill="var(--acc)"/></marker>'
		'<marker id="ahr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">'
		'<path d="M0 0 L8 4 L0 8 z" fill="var(--mute)"/></marker></defs>'
	)
	span = (w - 52) / len(lanes)
	X = [36 + span * (i + 0.5) for i in range(len(lanes))]
	hw = min(150, span - 16)
	for x, name in zip(X, lanes, strict=True):
		f.add(f'<rect class="hd" x="{x - hw / 2}" y="50" width="{hw}" height="30" rx="8"/>')
		f.add(t(x, 70, name, "head", "middle"))
		f.add(f'<line class="life" x1="{x}" x2="{x}" y1="80" y2="{h - 12}"/>')
	step = 80 / len(items)
	for i, item in enumerate(items):
		y = 112 + i * row
		at = round(3 + i * step)
		if item[0] == "note":
			_, lane, label = item
			bw = len(label) * 6.7 + 20
			nx = max(32, X[lane] - bw / 2)
			inner = f'<rect class="n" x="{nx}" y="{y - 14}" width="{bw}" height="24" rx="6"/>' + t(
				nx + bw / 2, y + 2, label, "code", "middle"
			)
			n_x = nx - 6
		else:
			kind, a, b, label = item
			d = 1 if X[b] > X[a] else -1
			inner = f'<line class="{kind}" x1="{X[a] + 4 * d}" x2="{X[b] - 6 * d}" y1="{y}" y2="{y}"/>' + t(
				(X[a] + X[b]) / 2, y - 8, label, "code", "middle"
			)
			n_x = min(X[a], X[b]) - 10
		inner += t(n_x, y + 4, str(i + 1), "step", "end")
		f.appear(inner, at)
	if status:
		lane, after, before, after_label = status
		y = 112 + after * row + 16
		flip = round(3 + (after + 1) * step)
		f.vanish(pill(X[lane] - 34, y, 68, before), flip)
		f.appear(pill(X[lane] - 34, y, 68, after_label, ok=True), flip)
	return f.svg()


def test_with_stubs():
	return sequence(
		4,
		"A test drives the stub",
		"The test calls pay() on the stub, then sync_status() on the request. The controller reads the status "
		"from the stub, and the request moves from Pending to Paid.",
		["Test", "Payment Request", "Settings controller", "StubRazorpayClient"],
		[
			("msg", 0, 3, "StubRazorpayClient.pay(link_id)"),
			("msg", 0, 1, "sync_status()"),
			("msg", 1, 2, "get_payment_status(order_ref)"),
			("msg", 2, 3, "get_payment_link(link_id)"),
			("ret", 3, 2, "status: paid"),
			("ret", 2, 1, '"Paid"'),
			("note", 0, 'assertEqual(status, "Paid")'),
		],
		status=(1, 5, "PENDING", "PAID"),
	)


def tabby_capture():
	return sequence(
		5,
		"Tabby: authorise, then capture",
		"Your app calls sync_status. BWH Payments reads the Tabby payment, finds it authorised, captures the "
		"authorised amount, reads it again as closed, and reports Paid.",
		["Your app", "BWH Payments", "Tabby"],
		[
			("msg", 0, 1, "sync_status()"),
			("msg", 1, 2, "GET /payments/{id}"),
			("ret", 2, 1, "authorized"),
			("msg", 1, 2, "POST …/captures  (authorised amount)"),
			("msg", 1, 2, "GET /payments/{id}"),
			("ret", 2, 1, "closed"),
			("ret", 1, 0, "Paid"),
		],
		status=(1, 5, "PENDING", "PAID"),
	)


FIGURES = {
	"bwhp-settle-flow": overview,  # get-started/overview
	"bwhp-service-clients": service_clients,  # build/service-clients, build/build-a-payment-gateway
	"bwhp-gateway-contract": contract,  # reference/gateway-contract
	"bwhp-test-with-stubs": test_with_stubs,  # build/test-with-stubs
	"bwhp-tabby-capture": tabby_capture,  # gateways/tabby
}

if __name__ == "__main__":
	# python3 -I figures.py OUT_DIR [name ...] [--still]
	# --still also writes OUT_DIR/still/<name>-<theme>.svg with animation off, on the page colour: the frame
	# readers with reduced motion see, and the only frame Quick Look can render.
	args = [a for a in sys.argv[1:] if a != "--still"]
	out, names = Path(args[0]), args[1:] or list(FIGURES)
	page = {"light": "#ffffff", "dark": "#171717"}
	for name in names:
		markup = FIGURES[name]()
		for theme, tokens in THEMES.items():
			themed = markup.replace("/*tokens*/", tokens)
			(out / f"{name}-{theme}.svg").write_text(themed)
			print(out / f"{name}-{theme}.svg")
			if "--still" in sys.argv:
				(out / "still").mkdir(exist_ok=True)
				still = themed.replace("</style>", ".a{animation:none!important}</style>")
				still = still.replace(
					"</title>", f'</title>\n<rect width="100%" height="100%" fill="{page[theme]}"/>', 1
				)
				(out / "still" / f"{name}-{theme}.svg").write_text(still)
