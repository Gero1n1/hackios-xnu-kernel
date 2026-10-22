#!/bin/bash
# Only prepares dependencies inside the CI runner's fresh, dedicated THEOS.
# Never downloads an IPSW or publishes Apple's extracted userland.
set -euo pipefail
: "${THEOS:?Set THEOS to a new dependency directory}"
test ! -e "$THEOS" || { echo 'Refusing to overwrite existing THEOS' >&2; exit 1; }
SDK_COMMIT=0222fd5413cf4b9af096f37b4621afa2688572f7
TOOLCHAIN_TAG=test-210562a
mkdir -p "$THEOS/toolchain" "$THEOS/sdks" ci-results
curl --fail --location --retry 3 --output "$THEOS/toolchain.tar.xz" \
  "https://github.com/L1ghtmann/llvm-project/releases/download/$TOOLCHAIN_TAG/iOSToolchain-x86_64.tar.xz"
sha256sum "$THEOS/toolchain.tar.xz" > ci-results/toolchain.sha256
tar -xJf "$THEOS/toolchain.tar.xz" -C "$THEOS/toolchain"
test -x "$THEOS/toolchain/linux/iphone/bin/ld"
git init -q "$THEOS/sdk-source"
git -C "$THEOS/sdk-source" remote add origin https://github.com/theos/sdks.git
git -C "$THEOS/sdk-source" config core.sparseCheckout true
printf '/iPhoneOS16.5.sdk/\n' > "$THEOS/sdk-source/.git/info/sparse-checkout"
git -C "$THEOS/sdk-source" fetch --depth 1 --filter=blob:none origin "$SDK_COMMIT"
git -C "$THEOS/sdk-source" -c advice.detachedHead=false checkout FETCH_HEAD
test "$(git -C "$THEOS/sdk-source" rev-parse HEAD)" = "$SDK_COMMIT"
ln -s "$THEOS/sdk-source/iPhoneOS16.5.sdk" "$THEOS/sdks/iPhoneOS16.5.sdk"
test -d "$THEOS/sdks/iPhoneOS16.5.sdk/usr/include"
printf 'sdk=%s\ntoolchain_tag=%s\n' "$SDK_COMMIT" "$TOOLCHAIN_TAG" > ci-results/dependencies.txt
