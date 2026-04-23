import os
import smtplib
import time
from html import escape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument


def _require_env(name: str) -> str:
	value = os.getenv(name)
	if not value:
		raise RuntimeError(f"Missing required environment variable: {name}")
	return value


def _to_object_id(value):
	if isinstance(value, ObjectId):
		return value
	if isinstance(value, str) and ObjectId.is_valid(value):
		return ObjectId(value)
	return None


def _extract_user_ids(relationship_refs):
	if not isinstance(relationship_refs, list):
		return []

	ids = []
	for ref in relationship_refs:
		if not isinstance(ref, dict):
			continue
		if ref.get("relationTo") != "users":
			continue

		object_id = _to_object_id(ref.get("value"))
		if object_id is not None:
			ids.append(object_id)

	return ids


def resolve_emails(users_collection, relationship_refs):
	user_ids = _extract_user_ids(relationship_refs)
	if not user_ids:
		return []

	users = users_collection.find({"_id": {"$in": user_ids}}, {"email": 1})
	emails_by_id = {
		user["_id"]: user.get("email")
		for user in users
		if user.get("email")
	}

	# Preserve the order from the communication document.
	return [email for user_id in user_ids if (email := emails_by_id.get(user_id))]


def serialize_slate_nodes(nodes):
	if not isinstance(nodes, list):
		return ""
	return "".join(serialize_slate_node(node) for node in nodes)


def serialize_slate_node(node):
	if not isinstance(node, dict):
		return escape(str(node))

	if "text" in node:
		text = escape(str(node.get("text", "")))
		if node.get("bold"):
			text = f"<strong>{text}</strong>"
		if node.get("italic"):
			text = f"<em>{text}</em>"
		return text

	node_type = node.get("type")
	children_html = "".join(serialize_slate_node(child) for child in node.get("children", []))

	if node_type == "paragraph":
		return f"<p>{children_html}</p>"
	if node_type == "h1":
		return f"<h1>{children_html}</h1>"
	if node_type == "h2":
		return f"<h2>{children_html}</h2>"
	if node_type == "ul":
		return f"<ul>{children_html}</ul>"
	if node_type == "li":
		return f"<li>{children_html}</li>"
	if node_type == "link":
		href = escape(str(node.get("url") or node.get("href") or "#"), quote=True)
		return f'<a href="{href}">{children_html}</a>'

	return children_html


def send_email(smtp_host, smtp_port, email_from, subject, to_emails, cc_emails, bcc_emails, html_body):
	message = MIMEMultipart("alternative")
	message["From"] = email_from
	message["Subject"] = str(subject or "")

	if to_emails:
		message["To"] = ", ".join(to_emails)
	if cc_emails:
		message["Cc"] = ", ".join(cc_emails)

	message.attach(MIMEText(html_body, "html", "utf-8"))

	recipients = to_emails + cc_emails + bcc_emails
	if not recipients:
		raise ValueError("Communication has no resolvable recipients")

	with smtplib.SMTP(smtp_host, smtp_port) as smtp:
		smtp.sendmail(email_from, recipients, message.as_string())


def process_communication(db, communication, smtp_host, smtp_port, email_from):
	users_collection = db["users"]

	to_emails = resolve_emails(users_collection, communication.get("tos"))
	cc_emails = resolve_emails(users_collection, communication.get("ccs"))
	bcc_emails = resolve_emails(users_collection, communication.get("bccs"))
	html_body = serialize_slate_nodes(communication.get("body", []))

	send_email(
		smtp_host=smtp_host,
		smtp_port=smtp_port,
		email_from=email_from,
		subject=communication.get("subject", ""),
		to_emails=to_emails,
		cc_emails=cc_emails,
		bcc_emails=bcc_emails,
		html_body=html_body,
	)


def main():
	load_dotenv()

	mongodb_uri = _require_env("MONGODB_URI")
	poll_interval_seconds = float(os.getenv("POLL_INTERVAL_SECONDS", "5"))
	smtp_host = _require_env("SMTP_HOST")
	smtp_port = int(_require_env("SMTP_PORT"))
	email_from = _require_env("EMAIL_FROM")

	client = MongoClient(mongodb_uri)
	default_db = client.get_default_database()
	db = default_db if default_db is not None else client["mzinga"]
	communications = db["communications"]

	print("Worker started. Polling communications with status='pending'.")

	while True:
		try:
			# Atomic claim to avoid two workers processing the same document.
			communication = communications.find_one_and_update(
				{"status": "pending"},
				{"$set": {"status": "processing"}},
				return_document=ReturnDocument.AFTER,
			)

			if communication is None:
				time.sleep(poll_interval_seconds)
				continue

			communication_id = communication["_id"]

			try:
				process_communication(
					db=db,
					communication=communication,
					smtp_host=smtp_host,
					smtp_port=smtp_port,
					email_from=email_from,
				)

				communications.update_one(
					{"_id": communication_id},
					{"$set": {"status": "sent"}},
				)
				print(f"Sent communication {communication_id}")
			except Exception as exc:
				communications.update_one(
					{"_id": communication_id},
					{"$set": {"status": "failed"}},
				)
				print(f"Failed communication {communication_id}: {exc}")
		except Exception as exc:
			print(f"Worker loop error: {exc}")
			time.sleep(poll_interval_seconds)


if __name__ == "__main__":
	main()
