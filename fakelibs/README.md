A tiny fake HTTP library in two versions, used by the demo instead of PyPI.

Changes from 1.0 to 2.0 (planted on purpose):
- fetch_all is removed
- get: parameter timeout renamed to timeout_s
- post: gains a required third parameter, headers
- retry: gains an optional parameter, backoff (harmless)
- stream is new
