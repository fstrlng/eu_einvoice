import re
from datetime import datetime
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

import frappe
from drafthorse.models.document import Document
from drafthorse.models.party import TaxRegistration
from drafthorse.models.payment import PaymentTerms
from drafthorse.models.tradelines import LineItem
from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice
from frappe.tests.utils import FrappeTestCase

from eu_einvoice.european_e_invoice.custom.sales_invoice import (
	EInvoiceGenerator,
	duty_tax_fee_category_codes,
	get_einvoice,
	get_item_rate,
	get_xml_attachment_file_base_name,
	vat_exemption_reason_codes,
)
from eu_einvoice.schematron import get_validation_errors
from eu_einvoice.utils import EInvoiceProfile, get_drafthorse_schema, get_guideline

NAMESPACES = {"ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"}
# Schematron rules about VAT categories, rates and exemption reasons
VAT_RULES = r"BR-(O|E|AE|G|IC|Z|S)-\d+|BR-4[5-9]|BR-DE-14|BR-CO-17"


class TestGetItemRate(FrappeTestCase):
	def _taxes(self):
		return [
			frappe._dict(account_head="VAT 19%", charge_type="On Net Total", rate=0),
			frappe._dict(account_head="VAT 7%", charge_type="On Net Total", rate=0),
		]

	def _template(self, rows):
		doc = MagicMock()
		doc.taxes = [frappe._dict(row) for row in rows]
		return doc

	def test_skips_zero_rate_without_not_applicable_field(self):
		template = self._template(
			[
				{"tax_type": "VAT 19%", "tax_rate": 0},
				{"tax_type": "VAT 7%", "tax_rate": 7},
			]
		)
		meta = MagicMock()
		meta.get_field.return_value = None
		with (
			patch("frappe.get_cached_doc", return_value=template),
			patch("frappe.get_meta", return_value=meta),
		):
			self.assertEqual(get_item_rate("7 %", self._taxes()), 7)

	def test_skips_not_applicable_row_when_field_exists(self):
		template = self._template(
			[
				{"tax_type": "VAT 19%", "tax_rate": 0, "not_applicable": 1},
				{"tax_type": "VAT 7%", "tax_rate": 7, "not_applicable": 0},
			]
		)
		meta = MagicMock()
		meta.get_field.return_value = MagicMock()
		with (
			patch("frappe.get_cached_doc", return_value=template),
			patch("frappe.get_meta", return_value=meta),
		):
			self.assertEqual(get_item_rate("7 %", self._taxes()), 7)

	def test_all_matching_rows_not_applicable_returns_zero(self):
		"""The template says no invoice tax applies, so the line is 0% - not the fallback rate."""
		template = self._template([{"tax_type": "VAT 19%", "tax_rate": 0, "not_applicable": 1}])
		meta = MagicMock()
		meta.get_field.return_value = MagicMock()
		taxes = [frappe._dict(account_head="VAT 19%", charge_type="On Net Total", rate=19)]
		with (
			patch("frappe.get_cached_doc", return_value=template),
			patch("frappe.get_meta", return_value=meta),
		):
			self.assertEqual(get_item_rate("Exempt", taxes), 0)

	def test_all_matching_rows_zero_rated_returns_zero_without_not_applicable_field(self):
		template = self._template([{"tax_type": "VAT 19%", "tax_rate": 0}])
		meta = MagicMock()
		meta.get_field.return_value = None
		taxes = [frappe._dict(account_head="VAT 19%", charge_type="On Net Total", rate=19)]
		with (
			patch("frappe.get_cached_doc", return_value=template),
			patch("frappe.get_meta", return_value=meta),
		):
			self.assertEqual(get_item_rate("Exempt", taxes), 0)

	def test_falls_back_to_single_tax_row_when_template_does_not_match(self):
		template = self._template([{"tax_type": "VAT 5%", "tax_rate": 5}])
		meta = MagicMock()
		meta.get_field.return_value = None
		taxes = [frappe._dict(account_head="VAT 19%", charge_type="On Net Total", rate=19)]
		with (
			patch("frappe.get_cached_doc", return_value=template),
			patch("frappe.get_meta", return_value=meta),
		):
			self.assertEqual(get_item_rate("5 %", taxes), 19)


class TestActualCharges(FrappeTestCase):
	def _four_charge_generator(self, profile=EInvoiceProfile.EN16931):
		generator = self._generator(profile)
		generator.invoice.taxes = []
		total = generator.invoice.net_total
		for description, amount in (("Packaging", 10), ("Freight", 20), ("Insurance", 5), ("Customs", 15)):
			total += amount
			generator.invoice.taxes.append(
				frappe._dict(
					account_head=description,
					charge_type="Actual",
					description=description,
					tax_amount=amount,
					total=total,
				)
			)
		generator.invoice.taxes.append(
			frappe._dict(
				account_head="VAT", charge_type="On Previous Row Total", row_id="4", rate=19, tax_amount=28.5
			)
		)
		return generator

	def _generator(self, profile=EInvoiceProfile.EN16931, rate=19, charge_type="On Previous Row Amount"):
		invoice = frappe._dict(
			net_total=100,
			tax_category=None,
			taxes_and_charges=None,
			precision=lambda fieldname: 2,
			taxes=[
				frappe._dict(
					account_head="VAT", charge_type="On Net Total", rate=rate, tax_amount=rate, net_amount=100
				),
				frappe._dict(
					account_head="Freight",
					charge_type="Actual",
					description="Freight",
					tax_amount=50,
					total=150,
				),
				frappe._dict(
					account_head="VAT", charge_type=charge_type, row_id="2", rate=rate, tax_amount=rate / 2
				),
			],
		)
		generator = EInvoiceGenerator(profile, invoice, None, None)
		generator.doc = Document()
		generator.item_tax_rates = {rate}
		return generator

	def test_actual_charge_xml_and_grouped_vat(self):
		for profile in (EInvoiceProfile.EN16931, EInvoiceProfile.XRECHNUNG, EInvoiceProfile.EXTENDED):
			with self.subTest(profile=profile):
				generator = self._generator(profile)
				with patch.object(duty_tax_fee_category_codes, "get", return_value="S"):
					self.assertTrue(generator._add_taxes_and_charges())
				root = generator.doc.trade.settlement.to_etree()
				self.assertIsNone(root.find("ram:SpecifiedLogisticsServiceCharge", NAMESPACES))
				charge = root.find("ram:SpecifiedTradeAllowanceCharge", NAMESPACES)
				self.assertEqual(charge.findtext("ram:ChargeIndicator/*", namespaces=NAMESPACES), "true")
				self.assertEqual(float(charge.findtext("ram:ActualAmount", namespaces=NAMESPACES)), 50)
				self.assertEqual(charge.findtext("ram:Reason", namespaces=NAMESPACES), "Freight")
				self.assertEqual(
					charge.findtext("ram:CategoryTradeTax/ram:CategoryCode", namespaces=NAMESPACES), "S"
				)
				self.assertEqual(
					float(
						charge.findtext(
							"ram:CategoryTradeTax/ram:RateApplicablePercent", namespaces=NAMESPACES
						)
					),
					19,
				)
				taxes = generator.doc.trade.settlement.trade_tax.children
				self.assertEqual(len(taxes), 1)
				self.assertEqual(taxes[0].basis_amount._value, 150)
				self.assertEqual(taxes[0].calculated_amount._value, 28.5)

	def test_charge_vat_uses_vat_account_mapping(self):
		generator = self._generator()
		with patch.object(
			duty_tax_fee_category_codes,
			"get",
			side_effect=lambda lookup: "S" if lookup[0] == ("Account", "VAT") else "E",
		):
			generator._add_taxes_and_charges()
		charge_tax = generator.doc.trade.settlement.allowance_charge.children[0].trade_tax.children[0]
		self.assertEqual(charge_tax.category_code._text, "S")

	def test_complete_en16931_invoice_validates(self):
		self._validate_complete_invoice(self._generator())

	def _validate_complete_invoice(self, generator):
		doc = generator.doc
		doc.context.guideline_parameter.id = get_guideline(generator.profile)
		doc.header.id = "ACTUAL-CHARGE-TEST"
		doc.header.type_code = "380"
		doc.header.issue_date_time = datetime(2026, 10, 6)
		for party, name in ((doc.trade.agreement.seller, "Seller"), (doc.trade.agreement.buyer, "Buyer")):
			party.name = name
			party.address.country_id = "DE"
		registration = TaxRegistration()
		registration.id = ("VA", "DE123456789")
		doc.trade.agreement.seller.tax_registrations.add(registration)
		line = LineItem()
		line.document.line_id = "1"
		line.product.name = "Product"
		line.agreement.net.amount = 100
		line.delivery.billed_quantity = (1, "C62")
		line.settlement.trade_tax.type_code = "VAT"
		line.settlement.trade_tax.category_code = "S"
		line.settlement.trade_tax.rate_applicable_percent = get_item_rate(None, generator.invoice.taxes)
		line.settlement.monetary_summation.total_amount = 100
		doc.trade.items.add(line)
		doc.trade.settlement.currency_code = "EUR"
		generator.invoice.update(currency="EUR", grand_total=178.5, outstanding_amount=178.5, total_advance=0)
		with patch.object(duty_tax_fee_category_codes, "get", return_value="S"):
			generator._add_taxes_and_charges()
		generator._set_totals()
		terms = PaymentTerms()
		terms.due = datetime(2026, 11, 6)
		doc.trade.settlement.terms.add(terms)
		xml = doc.serialize(schema=get_drafthorse_schema(generator.profile))
		errors, _warnings = get_validation_errors(xml.decode(), generator.profile)
		self.assertEqual(errors, [])

	def test_four_actual_charges_share_final_total_vat(self):
		for profile in (EInvoiceProfile.EN16931, EInvoiceProfile.XRECHNUNG, EInvoiceProfile.EXTENDED):
			with self.subTest(profile=profile):
				generator = self._four_charge_generator(profile)
				with patch.object(
					duty_tax_fee_category_codes,
					"get",
					side_effect=lambda lookup: "S" if lookup[0] == ("Account", "VAT") else "E",
				):
					self.assertTrue(generator._add_taxes_and_charges())
				charges = generator.doc.trade.settlement.allowance_charge.children
				self.assertEqual(
					[charge.reason._text for charge in charges],
					["Packaging", "Freight", "Insurance", "Customs"],
				)
				self.assertEqual([charge.actual_amount._value for charge in charges], [10, 20, 5, 15])
				for charge in charges:
					self.assertEqual(charge.trade_tax.children[0].category_code._text, "S")
					self.assertEqual(charge.trade_tax.children[0].rate_applicable_percent._value, 19)
				taxes = generator.doc.trade.settlement.trade_tax.children
				self.assertEqual(len(taxes), 1)
				self.assertEqual(taxes[0].basis_amount._value, 150)
				self.assertEqual(taxes[0].calculated_amount._value, 28.5)
				self.assertEqual(generator.doc.trade.settlement.service_charge.children, [])

	def test_complete_four_charge_en16931_invoice_validates(self):
		self._validate_complete_invoice(self._four_charge_generator())

	def test_four_charge_total_vat_defaults_to_last_row(self):
		generator = self._four_charge_generator()
		generator.invoice.taxes[-1].row_id = None
		self.assertEqual(get_item_rate(None, generator.invoice.taxes), 19)
		self._validate_complete_invoice(generator)

	def test_final_amount_vat_does_not_cover_earlier_charges(self):
		generator = self._four_charge_generator()
		generator.invoice.taxes[-1].charge_type = "On Previous Row Amount"
		self.assertIsNone(get_item_rate(None, generator.invoice.taxes))
		with patch.object(duty_tax_fee_category_codes, "get", return_value="S"):
			with self.assertRaises(frappe.ValidationError):
				generator._add_taxes_and_charges()

	def test_total_vat_reference_excludes_later_charges(self):
		generator = self._four_charge_generator()
		generator.invoice.taxes[-1].row_id = "2"
		self.assertIs(generator._get_actual_charge_vat_row(0), generator.invoice.taxes[-1])
		self.assertIs(generator._get_actual_charge_vat_row(1), generator.invoice.taxes[-1])
		self.assertIsNone(generator._get_actual_charge_vat_row(2))
		self.assertIsNone(generator._get_actual_charge_vat_row(3))
		self.assertIsNone(get_item_rate(None, generator.invoice.taxes))

	def test_total_vat_mapping_stops_at_intervening_tax(self):
		generator = self._four_charge_generator()
		generator.invoice.taxes.insert(
			2, frappe._dict(charge_type="On Net Total", rate=7, account_head="Other VAT")
		)
		generator.invoice.taxes[-1].row_id = "5"
		self.assertIsNone(generator._get_actual_charge_vat_row(0))
		self.assertIs(generator._get_actual_charge_vat_row(3), generator.invoice.taxes[-1])

	def test_different_vat_rates_remain_separate(self):
		generator = self._generator()
		generator.invoice.taxes[2].rate = 7
		generator.invoice.taxes[2].tax_amount = 3.5
		with patch.object(duty_tax_fee_category_codes, "get", return_value="S"):
			generator._add_taxes_and_charges()
		taxes = generator.doc.trade.settlement.trade_tax.children
		self.assertEqual([tax.rate_applicable_percent._value for tax in taxes], [19, 7])
		self.assertEqual([tax.basis_amount._value for tax in taxes], [100, 50])

	def test_previous_row_total_includes_line_and_charge_basis(self):
		generator = self._generator(charge_type="On Previous Row Total")
		generator.invoice.taxes = generator.invoice.taxes[1:]
		generator.invoice.taxes[1].row_id = "1"
		generator.invoice.taxes[1].tax_amount = 28.5
		with patch.object(duty_tax_fee_category_codes, "get", return_value="S"):
			generator._add_taxes_and_charges()
		tax = generator.doc.trade.settlement.trade_tax.children[0]
		self.assertEqual(tax.basis_amount._value, 150)
		self.assertEqual(tax.calculated_amount._value, 28.5)

	def test_not_subject_to_vat_charge_has_no_rate(self):
		generator = self._generator(rate=0)
		with (
			patch.object(duty_tax_fee_category_codes, "get", return_value="O"),
			patch.object(vat_exemption_reason_codes, "get", return_value="vatex-eu-o"),
		):
			generator._add_taxes_and_charges()
		charge = generator.doc.trade.settlement.allowance_charge.children[0].to_etree()
		self.assertIsNone(charge.find("ram:CategoryTradeTax/ram:RateApplicablePercent", NAMESPACES))

	def test_standard_charge_requires_explicit_vat(self):
		generator = self._generator()
		generator.invoice.taxes.pop()
		with patch.object(duty_tax_fee_category_codes, "get", return_value="S"):
			with self.assertRaises(frappe.ValidationError):
				generator._add_taxes_and_charges()

	def test_exempt_charge_without_vat_row_included_in_empty_tax_basis(self):
		generator = self._generator(rate=0)
		generator.invoice.taxes = [generator.invoice.taxes[1]]
		with (
			patch.object(duty_tax_fee_category_codes, "get", return_value="E"),
			patch.object(vat_exemption_reason_codes, "get", return_value="vatex-eu-79-c"),
		):
			self.assertFalse(generator._add_taxes_and_charges())
			generator._add_empty_tax()
		self.assertEqual(generator.doc.trade.settlement.trade_tax.children[0].basis_amount._value, 150)


class TestVatExemptionReason(FrappeTestCase):
	def test_empty_header_tax_skips_exemption_for_zero_rated(self):
		"""BR-Z-10: zero-rated VAT breakdown must not have an exemption reason."""
		invoice = frappe._dict(tax_category=None, taxes_and_charges=None, net_total=100)
		generator = EInvoiceGenerator(EInvoiceProfile.EN16931, invoice, None, None)
		generator.doc = Document()

		with patch.object(duty_tax_fee_category_codes, "get", return_value="Z"):
			generator._add_empty_tax()

		trade_tax = generator.doc.trade.settlement.trade_tax.children[0]
		self.assertEqual(trade_tax.category_code._text, "Z")
		self.assertEqual(trade_tax.rate_applicable_percent._value, 0)
		self.assertFalse(trade_tax.exemption_reason_code._text)

	def test_previous_row_vat_sets_exemption_reason(self):
		"""BR-*-10: VAT on an Actual charge also needs BT-120/BT-121 when exempt."""
		for charge_type, previous_field in (
			("On Previous Row Amount", "tax_amount"),
			("On Previous Row Total", "total"),
		):
			with self.subTest(charge_type=charge_type):
				previous = frappe._dict(
					account_head="_Test Freight",
					charge_type="Actual",
					description="Freight",
					tax_amount=50,
					total=50,
				)
				vat_row = frappe._dict(
					account_head="_Test Exempt VAT",
					charge_type=charge_type,
					rate=0,
					tax_amount=0,
				)
				invoice = frappe._dict(
					net_total=100,
					tax_category=None,
					taxes_and_charges=None,
					precision=lambda fieldname: 2,
					taxes=[previous, vat_row],
				)
				generator = EInvoiceGenerator(EInvoiceProfile.EXTENDED, invoice, None, None)
				generator.doc = Document()
				generator.item_tax_rates = set()

				with (
					patch.object(duty_tax_fee_category_codes, "get", return_value="E"),
					patch(
						"eu_einvoice.european_e_invoice.custom.sales_invoice.vat_exemption_reason_codes.get",
						return_value="vatex-eu-79-c",
					),
				):
					self.assertTrue(generator._add_taxes_and_charges())

				trade_tax = generator.doc.trade.settlement.trade_tax.children[0]
				self.assertEqual(trade_tax.type_code._text, "VAT")
				self.assertEqual(trade_tax.category_code._text, "E")
				self.assertEqual(trade_tax.basis_amount._value, getattr(previous, previous_field))
				self.assertEqual(trade_tax.exemption_reason_code._text, "VATEX-EU-79-C")


class TestNotSubjectToVatInvoice(FrappeTestCase):
	"""Generate and validate a real Sales Invoice with VAT category "O" (not subject to VAT)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		tax_category = frappe.get_doc(doctype="Tax Category", title="_Test Not Subject to VAT").insert()
		cls.tax_category = tax_category.name
		item_tax_template = frappe.get_doc(
			doctype="Item Tax Template",
			title="_Test Not Subject to VAT",
			company="_Test Company",
			taxes=[{"tax_type": "_Test Account VAT - _TC", "tax_rate": 0}],
		).insert()
		cls.item_tax_template = item_tax_template.name

		# The category comes from the Tax Category, the exemption reason only from the line's
		# Item Tax Template. So the VAT breakdown must pick it up from the line items.
		cls._map_code(duty_tax_fee_category_codes, "O", "Tax Category", cls.tax_category)
		cls._map_code(vat_exemption_reason_codes, "vatex-eu-o", "Item Tax Template", cls.item_tax_template)

	@staticmethod
	def _map_code(retriever, code: str, doctype: str, name: str):
		code_list = retriever.code_lists[0]
		if not frappe.db.exists("Code List", code_list):
			frappe.get_doc(doctype="Code List", __newname=code_list, title=code_list).insert()

		frappe.get_doc(
			doctype="Common Code",
			code_list=code_list,
			title=code,
			common_code=code,
			applies_to=[{"link_doctype": doctype, "link_name": name}],
		).insert()
		# ERPNext caches the code list lookup per request, and a test run is one request. Test records
		# can look up this code list before it exists, so clear the cached miss.
		frappe.clear_cache(doctype="Code List")

	def test_einvoice_for_category_o(self):
		for profile in (EInvoiceProfile.EN16931, EInvoiceProfile.XRECHNUNG):
			for with_tax_row in (False, True):
				with self.subTest(profile=profile, with_tax_row=with_tax_row):
					self._check_einvoice(profile, with_tax_row)

	def _check_einvoice(self, profile: EInvoiceProfile, with_tax_row: bool):
		invoice = create_sales_invoice(do_not_save=True, rate=100)
		invoice.einvoice_profile = profile.value
		invoice.tax_category = self.tax_category
		invoice.items[0].item_tax_template = self.item_tax_template
		if with_tax_row:
			invoice.append(
				"taxes",
				{
					"charge_type": "On Net Total",
					"account_head": "_Test Account VAT - _TC",
					"description": "VAT",
					"rate": 0,
				},
			)
		invoice.insert()

		xml = get_einvoice(invoice)

		root = ET.fromstring(xml)
		line_tax = root.find(".//ram:SpecifiedLineTradeSettlement/ram:ApplicableTradeTax", NAMESPACES)
		self.assertEqual(line_tax.findtext("ram:CategoryCode", namespaces=NAMESPACES), "O")
		# BR-O-05: no VAT rate on the line, not even 0
		self.assertIsNone(line_tax.find("ram:RateApplicablePercent", NAMESPACES))
		# The exemption reason belongs on the VAT breakdown only
		self.assertIsNone(line_tax.find("ram:ExemptionReasonCode", NAMESPACES))

		header_taxes = root.findall(
			".//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax", NAMESPACES
		)
		self.assertEqual(len(header_taxes), 1)
		header_tax = header_taxes[0]
		self.assertEqual(header_tax.findtext("ram:CategoryCode", namespaces=NAMESPACES), "O")
		self.assertEqual(header_tax.findtext("ram:ExemptionReasonCode", namespaces=NAMESPACES), "VATEX-EU-O")
		# BR-48 allows to omit the rate for category O, but XRechnung requires it (BR-DE-14)
		header_rate = header_tax.find("ram:RateApplicablePercent", NAMESPACES)
		if profile == EInvoiceProfile.XRECHNUNG:
			self.assertEqual(float(header_rate.text), 0)
		else:
			self.assertIsNone(header_rate)

		# The test company is not set up completely, so only check the VAT rules.
		# XRechnung builds on EN 16931, so check against both.
		for validation_profile in {profile, EInvoiceProfile.EN16931}:
			errors, _warnings = get_validation_errors(xml.decode(), validation_profile)
			vat_errors = [error for error in errors if re.search(VAT_RULES, error)]
			self.assertEqual(vat_errors, [])

	def test_tax_account_exemption_reason_wins_over_line(self):
		"""The tax row's own Account mapping is more specific than a reason from a line item."""
		account = frappe.get_doc(
			doctype="Account",
			account_name="_Test Not Subject to VAT",
			parent_account="Duties and Taxes - _TC",
			company="_Test Company",
			account_type="Tax",
		).insert()
		self._map_code(vat_exemption_reason_codes, "vatex-eu-g", "Account", account.name)

		invoice = create_sales_invoice(do_not_save=True, rate=100)
		invoice.tax_category = self.tax_category
		invoice.items[0].item_tax_template = self.item_tax_template
		invoice.append(
			"taxes",
			{"charge_type": "On Net Total", "account_head": account.name, "description": "VAT", "rate": 0},
		)
		invoice.insert()

		root = ET.fromstring(get_einvoice(invoice))
		header_tax = root.find(".//ram:ApplicableHeaderTradeSettlement/ram:ApplicableTradeTax", NAMESPACES)
		self.assertEqual(header_tax.findtext("ram:ExemptionReasonCode", namespaces=NAMESPACES), "VATEX-EU-G")


class TestApplicableTradeTaxes(FrappeTestCase):
	def test_multi_tax_invoice_groups_and_basis_amounts(self):
		"""Unused tax rows are dropped, the basis comes from net_amount before any fallback."""
		invoice = frappe._dict(
			net_total=1000,
			tax_category=None,
			taxes_and_charges=None,
			precision=lambda fieldname: 2,
			taxes=[
				# basis from the ERPNext tax row net_amount
				frappe._dict(
					account_head="_Test VAT 19",
					charge_type="On Net Total",
					rate=19,
					tax_amount=19,
					net_amount=100,
				),
				# basis from the custom field of older ERPNext versions
				frappe._dict(
					account_head="_Test VAT 7",
					charge_type="On Net Total",
					rate=7,
					tax_amount=7,
					custom_net_amount=100,
				),
				# net_amount exists as a field, but is empty, so it is calculated as well
				frappe._dict(
					account_head="_Test VAT 10",
					charge_type="On Net Total",
					rate=10,
					tax_amount=10,
					net_amount=0,
				),
				# no net amount known, so it is calculated from tax amount and rate
				frappe._dict(account_head="_Test VAT 5", charge_type="On Net Total", rate=5, tax_amount=0.5),
				# no tax amount, but a line item uses this rate (net amount 0), so it is kept
				frappe._dict(account_head="_Test VAT 3", charge_type="On Net Total", rate=3, tax_amount=0),
				# no tax amount and no line item uses this rate, so there is nothing to declare
				frappe._dict(account_head="_Test VAT 2", charge_type="On Net Total", rate=2, tax_amount=0),
			],
		)
		generator = EInvoiceGenerator(EInvoiceProfile.EN16931, invoice, None, None)
		generator.doc = Document()
		generator.item_tax_rates = {19, 7, 10, 5, 3}

		with patch.object(duty_tax_fee_category_codes, "get", return_value="S"):
			self.assertTrue(generator._add_taxes_and_charges())

		trade_taxes = generator.doc.trade.settlement.trade_tax.children
		self.assertEqual([t.rate_applicable_percent._value for t in trade_taxes], [19, 7, 10, 5, 3])
		self.assertEqual([t.basis_amount._value for t in trade_taxes], [100, 100, 100, 10, 0])


class TestPaymentExport(FrappeTestCase):
	def _generator(self, **values):
		invoice = frappe._dict(
			net_total=100,
			taxes=[frappe._dict(charge_type="On Net Total", tax_amount=19)],
			currency="EUR",
			party_account_currency="EUR",
			conversion_rate=1,
			grand_total=119,
			outstanding_amount=119,
			total_advance=0,
			posting_date="2026-10-06",
			payment_schedule=[],
		)
		invoice.update(values)
		generator = EInvoiceGenerator(EInvoiceProfile.XRECHNUNG, invoice, None, None)
		generator.doc = Document()
		return generator

	def test_payment_totals(self):
		for outstanding, advance, expected_paid in (
			(119, 0, 0),
			(69, 0, 50),
			(99, 20, 20),
			(49, 20, 70),
			(0, 0, 119),
		):
			with self.subTest(outstanding=outstanding, advance=advance):
				generator = self._generator(outstanding_amount=outstanding, total_advance=advance)
				generator._set_totals()
				summation = generator.doc.trade.settlement.monetary_summation
				self.assertEqual(float(summation.prepaid_total._value), expected_paid)
				self.assertEqual(float(summation.due_amount._value), outstanding)
				self.assertEqual(
					summation.grand_total._amount - summation.prepaid_total._value,
					summation.due_amount._value,
				)

	def test_outstanding_amount_is_converted_to_invoice_currency(self):
		generator = self._generator(
			currency="USD", party_account_currency="EUR", conversion_rate=0.8, outstanding_amount=55.2
		)
		generator._set_totals()
		summation = generator.doc.trade.settlement.monetary_summation
		self.assertEqual(float(summation.prepaid_total._value), 50)
		self.assertEqual(float(summation.due_amount._value), 69)

	def test_invoice_currency_outstanding_is_not_converted(self):
		generator = self._generator(
			currency="USD", party_account_currency="USD", conversion_rate=0.8, outstanding_amount=69
		)
		generator._set_totals()
		summation = generator.doc.trade.settlement.monetary_summation
		self.assertEqual(float(summation.prepaid_total._value), 50)
		self.assertEqual(float(summation.due_amount._value), 69)

	def _payment_description(self, discount_date, payment_amount=119, description=""):
		generator = self._generator(
			payment_schedule=[
				frappe._dict(
					description=description,
					due_date="2026-11-05",
					discount=2,
					discount_date=discount_date,
					discount_type="Percentage",
					payment_amount=payment_amount,
				)
			]
		)
		generator._add_payment_terms()
		return generator.doc.trade.settlement.terms.children[0].description._text

	def test_skonto_for_future_discount_date(self):
		self.assertEqual(
			self._payment_description("2026-10-16"), "#SKONTO#TAGE=10#PROZENT=2.00#\n"
		)

	def test_skonto_for_same_day(self):
		self.assertEqual(
			self._payment_description("2026-10-06"), "#SKONTO#TAGE=0#PROZENT=2.00#\n"
		)

	def test_skonto_before_posting_date_is_omitted(self):
		self.assertEqual(self._payment_description("2026-10-05", description="Payment terms"), "Payment terms")

	def test_skonto_preserves_description_and_partial_basis(self):
		self.assertEqual(
			self._payment_description("2026-10-16", payment_amount=59.5, description="Terms #1"),
			"Terms //1\n#SKONTO#TAGE=10#PROZENT=2.00#BASISBETRAG=59.50#\n",
		)


class TestXmlAttachmentNaming(FrappeTestCase):
	def test_auto_name_format_from_e_invoice_settings(self):
		doc = frappe._dict(name="SINV-00001", po_no="PO-42", doctype="Sales Invoice")
		field = "auto_name_format_for_xml_file"
		pattern = "XMLSTEM.-.{po_no}"
		real_gsv = frappe.get_single_value

		def get_single_value(doctype, fname, cache=True):
			if doctype == "E Invoice Settings" and fname == field:
				return pattern
			return real_gsv(doctype, fname, cache=cache)

		with patch.object(frappe, "get_single_value", side_effect=get_single_value):
			self.assertEqual(get_xml_attachment_file_base_name(doc), "XMLSTEM-PO-42")

	def test_empty_or_whitespace_uses_doc_name(self):
		doc = frappe._dict(name="SINV/00001", doctype="Sales Invoice")
		self.assertEqual(get_xml_attachment_file_base_name(doc, pattern=""), "SINV_00001")
		self.assertEqual(get_xml_attachment_file_base_name(doc, pattern="   "), "SINV_00001")

	def test_dot_separated_pattern_with_field(self):
		doc = frappe._dict(name="SINV-00001", po_no="PO-1", doctype="Sales Invoice")
		base = get_xml_attachment_file_base_name(doc, pattern="EXAMPLE.-.{po_no}")
		self.assertEqual(base, "EXAMPLE-PO-1")

	def test_dot_separated_inv_field_end(self):
		doc = frappe._dict(name="SINV-00001", po_no="X-9", doctype="Sales Invoice")
		base = get_xml_attachment_file_base_name(doc, pattern="INV.-.{po_no}.-.END")
		self.assertEqual(base, "INV-X-9-END")

	def test_leading_hashes_stripped_per_part_avoids_series_counter(self):
		doc = frappe._dict(name="SINV-00001", po_no="PO-1", doctype="Sales Invoice")
		base = get_xml_attachment_file_base_name(doc, pattern="EXAMPLE.-.{po_no}.#####")
		self.assertEqual(base, "EXAMPLE-PO-1")

	def test_hash_in_literal_sanitized_like_file_utils(self):
		doc = frappe._dict(name="SINV-00001", doctype="Sales Invoice")
		base = get_xml_attachment_file_base_name(doc, pattern="INV#X")
		self.assertEqual(base, "INV_X")

	def test_only_hash_segments_falls_back_to_doc_name(self):
		doc = frappe._dict(name="SINV-00001", doctype="Sales Invoice")
		self.assertEqual(get_xml_attachment_file_base_name(doc, pattern="#####"), "SINV-00001")

	def test_slash_in_resolved_base_is_sanitized(self):
		doc = frappe._dict(name="SINV-00001", po_no="A/B", doctype="Sales Invoice")
		base = get_xml_attachment_file_base_name(doc, pattern="{po_no}")
		self.assertEqual(base, "A_B")
