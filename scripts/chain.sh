#!/usr/bin/env bash
# Local FISCO BCOS 3.x consortium (4 nodes, PBFT) with Web3 RPC on ports 8545-8548.
#   scripts/chain.sh setup   download v3.17.1 and generate nodes (once)
#   scripts/chain.sh start | stop | status
set -euo pipefail

VERSION="v3.17.1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)/chain"
NODES="$ROOT/nodes/127.0.0.1"

setup() {
  mkdir -p "$ROOT/dist"
  cd "$ROOT/dist"
  base="https://github.com/FISCO-BCOS/FISCO-BCOS/releases/download/$VERSION"
  [ -f build_chain.sh ] || curl -fsSL -o build_chain.sh "$base/build_chain.sh"
  [ -x fisco-bcos ] || { curl -fsSL -o fisco.tgz "$base/fisco-bcos-linux-x86_64.tar.gz" && tar xzf fisco.tgz; }
  cd "$ROOT"
  [ -d nodes ] || bash dist/build_chain.sh -l 127.0.0.1:4 -p 30300,20200 -e dist/fisco-bcos -o nodes
  for i in 0 1 2 3; do
    python3 - "$NODES/node$i/config.ini" "$i" <<'EOF'
import re, sys
path, i = sys.argv[1], int(sys.argv[2])
s = open(path).read()
s = re.sub(r"(\[web3_rpc\]\s*\n\s*enable=)false", r"\1true", s)
s = re.sub(r"(\[web3_rpc\][^\[]*?listen_ip=)0\.0\.0\.0", r"\g<1>127.0.0.1", s, flags=re.S)
s = re.sub(r"(\[web3_rpc\][^\[]*?listen_port=)\d+", rf"\g<1>{8545 + i}", s, flags=re.S)
open(path, "w").write(s)
EOF
  done
  echo "nodes ready in $NODES (Web3 RPC on 127.0.0.1:8545-8548)"
}

case "${1:-}" in
  setup) setup ;;
  start) bash "$NODES/start_all.sh" ;;
  stop) bash "$NODES/stop_all.sh" ;;
  status) curl -s -X POST 127.0.0.1:8545 -H 'Content-Type: application/json' \
            -d '{"jsonrpc":"2.0","method":"eth_blockNumber","params":[],"id":1}' && echo ;;
  *) echo "usage: $0 setup|start|stop|status" >&2; exit 1 ;;
esac
