import frappe
from frappe import _
from frappe.utils.data import cint, flt

# ISO 4217 minor-unit exponents for every currency that is not the 2-decimal default. Gateways bill in
# minor units, so a wrong exponent is a 10x or 100x mischarge. The Currency doctype's `fraction_units`
# is site-editable data and is wrong out of the box (it reads 100 for JPY, a zero-decimal currency), so
# the published standard is pinned here instead of read from the database.
MINOR_UNIT_EXPONENTS = {
	"BHD": 3,
	"BIF": 0,
	"CLF": 4,
	"CLP": 0,
	"DJF": 0,
	"GNF": 0,
	"IQD": 3,
	"ISK": 0,
	"JOD": 3,
	"JPY": 0,
	"KMF": 0,
	"KRW": 0,
	"KWD": 3,
	"LYD": 3,
	"OMR": 3,
	"PYG": 0,
	"RWF": 0,
	"TND": 3,
	"UGX": 0,
	"UYI": 0,
	"UYW": 4,
	"VND": 0,
	"VUV": 0,
	"XAF": 0,
	"XOF": 0,
	"XPF": 0,
}
DEFAULT_MINOR_UNIT_EXPONENT = 2


def get_minor_unit_exponent(currency: str) -> int:
	if not currency:
		frappe.throw(_("Currency is required to convert an amount to gateway minor units"))
	return MINOR_UNIT_EXPONENTS.get(currency.strip().upper(), DEFAULT_MINOR_UNIT_EXPONENT)


def to_minor_units(amount: float, currency: str) -> int:
	"""Convert a major-unit amount to the integer minor units a gateway charges in."""
	exponent = get_minor_unit_exponent(currency)
	return cint(round(flt(amount, exponent) * (10**exponent)))


def from_minor_units(amount: int, currency: str) -> float:
	"""Inverse of `to_minor_units`, so a gateway's echoed amount compares equal to what we sent."""
	exponent = get_minor_unit_exponent(currency)
	return flt(cint(amount) / (10**exponent), exponent)
