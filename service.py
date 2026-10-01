"""鲜切花冷链履约中枢的基础服务入口。"""

import argparse
from http.server import ThreadingHTTPServer

from coldchain import AppendOnlyStore
from coldchain.api import make_handler

SERVICE_ID = "flower-cold-chain"
SERVICE_NAME = "鲜切花冷链履约中枢"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default=None, help="JSONL 持久化文件路径（可选）")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        print("基础检查通过")
        return
    store = AppendOnlyStore(args.data)
    handler = make_handler(store, health_payload)
    ThreadingHTTPServer(("0.0.0.0", args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
