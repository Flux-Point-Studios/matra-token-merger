#!/usr/bin/env bash
# Install the pinned cardano-cli 11.0.0.0 as DIR/cardano-cli, root-owned 0755,
# from the IntersectMBO release, refusing a tarball or binary whose sha256
# differs from the pins.
#
#   sudo services/deploy/install-cardano-cli.sh /opt/cardano-cli-11.0.0.0
set -euo pipefail

dir=${1:?usage: install-cardano-cli.sh DIR}
url=https://github.com/IntersectMBO/cardano-cli/releases/download/cardano-cli-11.0.0.0/cardano-cli-11.0.0.0-x86_64-linux.tar.gz
tarball_sha256=c37b26be04106ecd2ba88841e64d3b7ecdc80d838055102323691b087f7255b1
binary_sha256=b55687eb503a26a96dd436cdb492cbee15a945588c346b81dc4ca86f3c8a1066

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl --proto '=https' --tlsv1.2 -sSLf -o "$work/cli.tgz" "$url"
echo "$tarball_sha256  $work/cli.tgz" | sha256sum -c --quiet -
tar -xzf "$work/cli.tgz" -C "$work" cardano-cli-x86_64-linux
echo "$binary_sha256  $work/cardano-cli-x86_64-linux" | sha256sum -c --quiet -
install -D -m 0755 -o root -g root "$work/cardano-cli-x86_64-linux" "$dir/cardano-cli"
echo "installed cardano-cli 11.0.0.0 at $dir/cardano-cli (sha256 $binary_sha256)"
