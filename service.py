"""鲜切花冷链履约中枢的服务入口。

/health            基础身份检查（无需令牌）。
/api/...           冷链测量治理接口，见 coldchain.api。
"""

import argparse
from http.server import ThreadingHTTPServer

from coldchain.api import make_server
from coldchain.store import Store

SERVICE_ID = "flower-cold-chain"
SERVICE_NAME = "鲜切花冷链履约中枢"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_server(port=8000, db_path=":memory:", seed_dev_tokens=False):
    return make_server(store=Store(db_path), port=port,
                       seed_dev_tokens=seed_dev_tokens, health=health_payload)


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="coldchain.db", help="SQLite 路径")
    parser.add_argument("--seed-dev-tokens", action="store_true",
                        help="写入开发用演示令牌（仅限非生产环境）")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        Store(":memory:").close()
        print("基础检查通过")
        return
    server, _app = build_server(args.port, args.db, args.seed_dev_tokens)
    server.serve_forever()


if __name__ == "__main__":
    main()
