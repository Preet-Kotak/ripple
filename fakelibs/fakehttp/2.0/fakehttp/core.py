def get(url, timeout_s=10):
    return {"url": url, "timeout": timeout_s}


def post(url, data, headers):
    return {"url": url, "data": data, "headers": headers}


def head(url):
    return {"url": url}


def stream(url):
    yield get(url)


def retry(fn, times, backoff=1.0):
    for _ in range(times):
        try:
            return fn()
        except Exception:
            pass


class Session:
    def __init__(self, base_url):
        self.base_url = base_url

    def close(self):
        pass
