def get(url, timeout=10):
    return {"url": url, "timeout": timeout}


def post(url, data):
    return {"url": url, "data": data}


def head(url):
    return {"url": url}


def fetch_all(urls):
    return [get(u) for u in urls]


def retry(fn, times):
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
