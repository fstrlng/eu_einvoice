from functools import cached_property

import frappe
from frappe.email.doctype.email_queue.email_queue import EmailQueue

from eu_einvoice.european_e_invoice.custom.sales_invoice import attach_xml_to_pdf


def get_print_attachment(attachment: dict) -> dict:
	"""Create a print attachment and embed XML for Sales Invoice PDFs."""
	print_attachment = frappe.attach_print(**attachment)
	if attachment.get("doctype") != "Sales Invoice" or not print_attachment["fname"].lower().endswith(".pdf"):
		return print_attachment

	try:
		print_attachment["fcontent"] = attach_xml_to_pdf(attachment["name"], print_attachment["fcontent"])
	except Exception:
		# Do not block an email if e-invoice generation or embedding fails.
		frappe.log_error(f"Error attaching XML to email PDF for Sales Invoice {attachment['name']}")

	return print_attachment


class EInvoiceEmailQueue(EmailQueue):
	"""Embed e-invoice XML when the mail queue creates a Sales Invoice PDF."""

	@cached_property
	def attachments(self) -> list[dict]:
		attachments = []

		for attachment in self.attachments_list:
			if attachment.get("fcontent"):
				continue
			file_filters = {}
			if attachment.get("fid"):
				file_filters["name"] = attachment["fid"]
			elif attachment.get("file_url"):
				file_filters["file_url"] = attachment["file_url"]

			if file_filters:
				file = frappe.get_doc("File", file_filters)
				attachment.update({"fname": file.file_name, "fcontent": file.get_content()})
				attachment.pop("fid", None)
				attachment.pop("file_url", None)
				attachments.append(attachment)
			elif attachment.get("print_format_attachment") == 1:
				attachment.pop("print_format_attachment", None)
				print_attachment = get_print_attachment(attachment)
				self._store_file(print_attachment["fname"], print_attachment["fcontent"])
				attachments.append(print_attachment)

		return attachments
