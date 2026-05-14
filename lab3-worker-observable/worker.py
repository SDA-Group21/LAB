import logging
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
import structlog
from dotenv import load_dotenv
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import start_http_server

load_dotenv()

MZINGA_URL = os.environ["MZINGA_URL"]
MZINGA_EMAIL = os.environ["MZINGA_EMAIL"]
MZINGA_PASSWORD = os.environ["MZINGA_PASSWORD"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", 5))
SMTP_HOST = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT = int(os.getenv("SMTP_PORT", 1025))
EMAIL_FROM = os.getenv("EMAIL_FROM", "worker@mzinga.io")
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
SERVICE_NAME_VALUE = os.getenv("OTEL_SERVICE_NAME", "email-worker")
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", 8000))

resource = Resource(
    attributes={
        SERVICE_NAME: SERVICE_NAME_VALUE,
        SERVICE_VERSION: "1.0.0",
    }
)

tracer_provider = TracerProvider(resource=resource)
otlp_exporter = OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT}/v1/traces")
tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
trace.set_tracer_provider(tracer_provider)

RequestsInstrumentor().instrument()

tracer = trace.get_tracer(SERVICE_NAME_VALUE)

start_http_server(port=PROMETHEUS_PORT)

metric_reader = PrometheusMetricReader()
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)

meter = metrics.get_meter(SERVICE_NAME_VALUE)

emails_processed = meter.create_counter(
    name="emails_processed_total",
    description="Total number of communications processed",
    unit="1",
)
processing_duration = meter.create_histogram(
    name="email_processing_duration_seconds",
    description="End-to-end duration of processing one communication",
    unit="s",
)
smtp_duration = meter.create_histogram(
    name="smtp_send_duration_seconds",
    description="Duration of the SMTP send call",
    unit="s",
)
poll_counter = meter.create_counter(
    name="worker_poll_total",
    description="Number of poll cycles",
    unit="1",
)


def add_otel_context(logger, method, event_dict):
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        add_otel_context,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)
log = structlog.get_logger(service=SERVICE_NAME_VALUE)


def login() -> str:
    resp = requests.post(
        f"{MZINGA_URL}/api/users/login",
        json={"email": MZINGA_EMAIL, "password": MZINGA_PASSWORD},
    )
    resp.raise_for_status()
    log.info("authenticated")
    return resp.json()["token"]


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def fetch_pending(token: str) -> list:
    resp = requests.get(
        f"{MZINGA_URL}/api/communications",
        params={"where[status][equals]": "pending", "depth": 1},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json().get("docs", [])


def fetch_doc(token: str, doc_id: str) -> dict:
    resp = requests.get(
        f"{MZINGA_URL}/api/communications/{doc_id}",
        params={"depth": 1},
        headers=auth_headers(token),
    )
    resp.raise_for_status()
    return resp.json()


def update_status(token: str, doc_id: str, status: str):
    resp = requests.patch(
        f"{MZINGA_URL}/api/communications/{doc_id}",
        json={"status": status},
        headers=auth_headers(token),
    )
    resp.raise_for_status()


def slate_to_html(nodes: list) -> str:
    html = ""
    for node in nodes or []:
        if node.get("type") == "paragraph":
            html += f"<p>{slate_to_html(node.get('children', []))}</p>"
        elif node.get("type") == "h1":
            html += f"<h1>{slate_to_html(node.get('children', []))}</h1>"
        elif node.get("type") == "h2":
            html += f"<h2>{slate_to_html(node.get('children', []))}</h2>"
        elif node.get("type") == "ul":
            html += f"<ul>{slate_to_html(node.get('children', []))}</ul>"
        elif node.get("type") == "li":
            html += f"<li>{slate_to_html(node.get('children', []))}</li>"
        elif node.get("type") == "link":
            url = node.get("url", "#")
            html += f'<a href="{url}">{slate_to_html(node.get("children", []))}</a>'
        elif "text" in node:
            text = node["text"]
            if node.get("bold"):
                text = f"<strong>{text}</strong>"
            if node.get("italic"):
                text = f"<em>{text}</em>"
            html += text
        else:
            html += slate_to_html(node.get("children", []))
    return html


def extract_emails(relationship_list: list) -> list[str]:
    emails = []
    for r in relationship_list or []:
        value = r.get("value") or {}
        if isinstance(value, dict) and value.get("email"):
            emails.append(value["email"])
    return emails


def send_email(
    to_addresses: list[str],
    subject: str,
    html: str,
    cc_addresses: list[str] = None,
    bcc_addresses: list[str] = None,
):
    with tracer.start_as_current_span("send_email") as span:
        span.set_attribute("recipient_count", len(to_addresses))
        t0 = time.perf_counter()

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = ", ".join(to_addresses)
        if cc_addresses:
            msg["Cc"] = ", ".join(cc_addresses)
        msg.attach(MIMEText(html, "html"))

        all_recipients = to_addresses + (cc_addresses or []) + (bcc_addresses or [])
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
                server.sendmail(EMAIL_FROM, all_recipients, msg.as_string())
        finally:
            smtp_duration.record(time.perf_counter() - t0)


def process(token: str, doc: dict) -> str:
    doc_id = doc["id"]
    structlog.contextvars.bind_contextvars(doc_id=doc_id)

    try:
        with tracer.start_as_current_span("process_communication") as span:
            span.set_attribute("doc_id", doc_id)
            t0 = time.perf_counter()
            recipient_count = 0

            update_status(token, doc_id, "processing")
            log.info("processing_started")

            try:
                to_emails = extract_emails(doc.get("tos"))
                recipient_count = len(to_emails)
                if not to_emails:
                    raise ValueError("No valid 'to' email addresses found")
                cc_emails = extract_emails(doc.get("ccs"))
                bcc_emails = extract_emails(doc.get("bccs"))

                with tracer.start_as_current_span("serialize_body") as serialize_span:
                    nodes = doc.get("body") or []
                    serialize_span.set_attribute("node_count", len(nodes))
                    html = slate_to_html(nodes)

                send_email(to_emails, doc["subject"], html, cc_emails, bcc_emails)
                update_status(token, doc_id, "sent")

                duration = time.perf_counter() - t0
                emails_processed.add(
                    1,
                    {"status": "sent", "recipient_count": recipient_count},
                )
                log.info(
                    "processing_completed",
                    status="sent",
                    recipient_count=recipient_count,
                    duration_s=round(duration, 3),
                )

            except Exception as e:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                update_status(token, doc_id, "failed")
                duration = time.perf_counter() - t0
                emails_processed.add(
                    1,
                    {"status": "failed", "recipient_count": recipient_count},
                )
                log.error(
                    "processing_failed",
                    status="failed",
                    recipient_count=recipient_count,
                    duration_s=round(duration, 3),
                    error=str(e),
                )
            finally:
                processing_duration.record(time.perf_counter() - t0)
    finally:
        structlog.contextvars.unbind_contextvars("doc_id")

    return token


def poll():
    token = login()
    log.info(
        "worker_started",
        poll_interval_s=POLL_INTERVAL,
        prometheus_port=PROMETHEUS_PORT,
    )

    while True:
        try:
            docs = fetch_pending(token)
            if docs:
                poll_counter.add(1, {"result": "found"})
                for doc in docs:
                    token = process(token, doc)
            else:
                poll_counter.add(1, {"result": "empty"})
                time.sleep(POLL_INTERVAL)
        except requests.HTTPError as e:
            if e.response.status_code == 401:
                log.warning("token_expired_reauthenticating")
                token = login()
            else:
                log.error(
                    "http_error",
                    status_code=e.response.status_code,
                    error=str(e),
                )
                time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    poll()
