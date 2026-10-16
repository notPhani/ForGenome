"""Shared HTTP retry policy for every connector.

Written because a nine-hour unattended harvest exposed something a 114-second single-gene
run never could: transient upstream failures are not rare, they are the normal case at
volume. The first panel smoke test hit `503 Service Unavailable` from Ensembl's REST API
on 2 of 6 VEP enrichments -- not because the requests were wrong (the same coordinates
succeed on retry) but because these are shared public endpoints that shed load. Without a
retry policy, an overnight run converts every one of those into a permanently dirty node,
and the retry ledger fills up with thousands of tickets that would have resolved on a
second attempt one second later.

Deliberately narrow in what it retries:

  RETRY   429 (rate limited), 500/502/503/504 (upstream transient), and connection or
          read timeouts. These say "ask again", and every one of them is a real,
          observed response from the four APIs in this stack.

  DO NOT  400/404/422 and friends. A 404 from NCBI's refsnp endpoint means that rsID does
          not exist -- retrying it three times is three times the load for the same
          answer, and worse, it disguises a permanent "no such record" as a transient
          failure in the ledger, which is exactly the distinction ingest/dirty.py exists
          to preserve.

`Retry-After` is honoured when the server sends one (Ensembl does), because a server
telling you when to come back is better information than any backoff formula. Otherwise
exponential backoff with jitter -- jittered because a harvest issues its requests in a
tight loop, so un-jittered retries from a stalled batch all fire at the same instant and
re-create the overload that caused the 503.
"""
from __future__ import annotations

import random
import time

import requests

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
DEFAULT_ATTEMPTS = 4
DEFAULT_BACKOFF = 1.0      # seconds; doubles each attempt
MAX_BACKOFF = 30.0


class RetryExhausted(requests.HTTPError):
    """Raised after the last attempt fails. Subclasses HTTPError so existing callers that
    catch HTTPError (and the runner's blanket except, which turns it into a retry ticket)
    keep behaving exactly as they did."""


def _sleep_for(response: requests.Response | None, attempt: int, backoff: float) -> float:
    if response is not None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                return min(float(header), MAX_BACKOFF)
            except ValueError:
                pass   # Retry-After may be an HTTP-date; fall through to backoff
    delay = min(backoff * (2 ** attempt), MAX_BACKOFF)
    return delay * (0.5 + random.random() * 0.5)   # jitter: 50-100% of the nominal delay


def request_with_retry(method: str, url: str, *, attempts: int = DEFAULT_ATTEMPTS,
                        backoff: float = DEFAULT_BACKOFF, timeout: float = 30.0,
                        **kwargs) -> requests.Response:
    """A `requests` call that survives transient upstream failure. Returns the response
    with `raise_for_status()` already applied, so callers keep their existing shape."""
    last_error: Exception | None = None
    for attempt in range(attempts):
        response = None
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            if response.status_code in RETRY_STATUS and attempt < attempts - 1:
                time.sleep(_sleep_for(response, attempt, backoff))
                continue
            response.raise_for_status()
            return response
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(_sleep_for(None, attempt, backoff))
                continue
        except requests.HTTPError as exc:
            # A non-retryable status that raise_for_status() surfaced -- 404, 400, 422.
            # Permanent: hand it straight back so the ledger records it as permanent.
            raise exc

    if last_error is not None:
        raise RetryExhausted(f"{method} {url}: {attempts} attempts failed, "
                             f"last error {type(last_error).__name__}: {last_error}")
    raise RetryExhausted(f"{method} {url}: {attempts} attempts failed, "
                          f"last status {response.status_code if response is not None else '?'}")


def get(url: str, **kwargs) -> requests.Response:
    return request_with_retry("GET", url, **kwargs)


def post(url: str, **kwargs) -> requests.Response:
    return request_with_retry("POST", url, **kwargs)
