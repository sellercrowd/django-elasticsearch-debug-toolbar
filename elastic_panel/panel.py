import hashlib
import json
import threading

from debug_toolbar.panels import Panel
from debug_toolbar.utils import get_stack, render_stacktrace, tidy_stacktrace
from django.templatetags.static import static
from django.utils.translation import gettext_lazy as _
from elastic_transport import Transport


class ThreadCollector:
    def __init__(self):
        self.data = threading.local()
        self.data.collection = []

    def collect(self, item):
        self.data.collection.append(item)

    def get_collection(self):
        return getattr(self.data, "collection", [])

    def clear_collection(self):
        self.data.collection = []


# Patching of the original elasticsearch log_request
old_perform_request = Transport.perform_request
collector = ThreadCollector()


def patched_perform_request(
    self,
    method: str,
    target: str,
    *,
    body,
    headers,
    max_retries,
    retry_on_status,
    retry_on_timeout,
    request_timeout,
    client_meta,
    otel_span,
):
    response = old_perform_request(
        self,
        method,
        target,
        body=body,
        headers=headers,
        max_retries=max_retries,
        retry_on_status=retry_on_status,
        retry_on_timeout=retry_on_timeout,
        request_timeout=request_timeout,
        client_meta=client_meta,
        otel_span=otel_span,
    )
    collector.collect(
        ElasticQueryInfo(
            method,
            f"{response.meta.node.scheme}://{response.meta.node.host}{target}",
            target,
            json.dumps(body),
            response.meta.status,
            json.dumps(response.body),
            response.meta.duration,
        )
    )
    return response


Transport.perform_request = patched_perform_request


def _pretty_json(data):
    # pretty JSON in tracer curl logs
    try:
        return json.dumps(json.loads(data), sort_keys=True, indent=2, separators=(",", ": ")).replace("'", r"\u0027")
    except (ValueError, TypeError):
        # non-json data or a bulk request
        return data


class ElasticQueryInfo:
    def __init__(self, method, full_url, path, body, status_code, response, duration):
        if not body:
            self.body = ""  # Python 3 TypeError if None
        else:
            self.body = _pretty_json(body)
            if isinstance(self.body, bytes):
                self.body = self.body.decode("ascii", "ignore")
        self.method = method
        self.full_url = full_url
        self.path = path
        self.status_code = status_code
        self.response = _pretty_json(response)
        self.duration = round(duration * 1000, 2)
        self.hash = hashlib.md5(
            self.full_url.encode("ascii", "ignore") + self.body.encode("ascii", "ignore")
        ).hexdigest()
        self.stacktrace = tidy_stacktrace(reversed(get_stack()))


class ElasticDebugPanel(Panel):
    """
    Panel that displays queries made by Elasticsearch backends.
    """

    name = "Elasticsearch"
    template = "elastic_panel/elastic_panel.html"
    has_content = True
    total_time = 0
    nb_duplicates = 0
    nb_queries = 0

    @property
    def nav_title(self):
        return _("Elastic Queries")

    @property
    def nav_subtitle(self):
        default_str = "{} queries {:.2f}ms".format(self.nb_queries, self.total_time)
        if self.nb_duplicates > 0:
            default_str += " {} DUPE".format(self.nb_duplicates)
        return default_str

    @property
    def title(self):
        return self.nav_title

    @property
    def scripts(self):
        scripts = super().scripts
        scripts.append(static("elastic_panel/js/elastic_panel.js"))
        return scripts

    def process_request(self, request):
        collector.clear_collection()
        return super().process_request(request)

    def generate_stats(self, request, response):
        records = collector.get_collection()
        self.total_time = 0
        self.nb_duplicates = 0

        hashs = set()
        for record in records:
            self.total_time += record.duration
            if record.hash in hashs:
                self.nb_duplicates += 1
            hashs.add(record.hash)
            record.stacktrace = render_stacktrace(record.stacktrace)

        self.nb_queries = len(records)

        collector.clear_collection()
        self.record_stats({"records": records})
