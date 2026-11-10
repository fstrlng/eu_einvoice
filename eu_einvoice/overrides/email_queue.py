from functools import cached_property

import frappe
from frappe.email.doctype.email_queue.email_queue import EmailQueue, SendMailContext
from frappe.utils import get_hook_method

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


class EInvoiceSendMailContext(SendMailContext):
	"""Embed e-invoice XML when the mail queue creates a Sales Invoice PDF."""

	@cached_property
	def attachments(self) -> list[dict]:
		attachments = []

		for attachment in self.queue_doc.attachments_list:
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


class EInvoiceEmailQueue(EmailQueue):
	"""Use the e-invoice-aware mail context when sending queued emails."""

	def send(self, smtp_server_instance=None, force_send: bool = False):
		if not self.can_send_now() and not force_send:
			return

		with EInvoiceSendMailContext(self, smtp_server_instance) as context:
			context.fetch_smtp_server()
			message = None
			for recipient in self.recipients:
				if recipient.is_mail_sent():
					continue
				message = context.build_message(recipient.recipient)
				if method := get_hook_method("override_email_send"):
					method(self, self.sender, recipient.recipient, message)
				elif not frappe.flags.in_test or frappe.flags.testing_email:
					context.smtp_server.session.sendmail(
						from_addr=self.sender,
						to_addrs=recipient.recipient,
						msg=message.decode("utf-8").encode(),
					)

				context.update_recipient_status_to_sent(recipient)

			if frappe.flags.in_test and not frappe.flags.testing_email:
				frappe.flags.sent_mail = message
				return

			if context.email_account_doc.append_emails_to_sent_folder:
				context.email_account_doc.append_email_to_sent_folder(message)
