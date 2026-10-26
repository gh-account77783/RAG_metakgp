"""Bounded loopback health probe; never authenticates or invokes a model."""
import sys
import urllib.request


def main():
    paths = {'reader': 'http://127.0.0.1:18000/health/live',
             'chroma': 'http://127.0.0.1:18001/api/v2/heartbeat'}
    try:
        url = paths[sys.argv[1]]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=5) as response:
            if response.status != 200 or len(response.read(65537)) > 65536:
                raise ValueError('Invalid health response')
    except (OSError, ValueError, KeyError, IndexError):
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
