#!/bin/bash

# Release script for Ally Center
# Updates version numbers, builds, packages, and optionally publishes to GitHub.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo -e "${GREEN}=== Ally Center Release Script ===${NC}"
echo ""

# sed -i takes an argument on BSD/macOS but not on GNU/Linux
if sed --version >/dev/null 2>&1; then
    sed_inplace() { sed -i "$@"; }
else
    sed_inplace() { local e="$1"; shift; sed -i '' "$e" "$@"; }
fi

# The lockfile is pnpm's, but don't hard-fail if only npm is present
if command -v pnpm >/dev/null 2>&1; then
    PKG_RUN="pnpm run"
elif command -v npm >/dev/null 2>&1; then
    PKG_RUN="npm run"
    echo -e "${YELLOW}pnpm not found, using npm${NC}"
else
    echo -e "${RED}Error: neither pnpm nor npm found${NC}"
    exit 1
fi

CURRENT=$(grep -m1 '"version"' package.json | sed 's/.*"version": "\(.*\)".*/\1/')
read -r -p "Enter version number [${CURRENT}]: " VERSION
VERSION="${VERSION:-$CURRENT}"

if [[ ! $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo -e "${RED}Error: Invalid version format. Use semantic versioning (e.g. 1.2.0)${NC}"
    exit 1
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo -e "${YELLOW}Warning: working tree has uncommitted changes.${NC}"
    read -r -p "Continue anyway? (y/N): " -n 1 REPLY; echo ""
    [[ $REPLY =~ ^[Yy]$ ]] || exit 1
fi

echo ""
echo -e "${YELLOW}Updating version to ${VERSION}...${NC}"

sed_inplace "s/\"version\": \"[0-9]*\.[0-9]*\.[0-9]*\"/\"version\": \"${VERSION}\"/" package.json
echo -e "${GREEN}✓ package.json${NC}"

# Keep the About modal honest - it is the only version a user actually sees
sed_inplace "s/Version [0-9]*\.[0-9]*\.[0-9]*/Version ${VERSION}/" src/index.tsx
if ! grep -q "Version ${VERSION}" src/index.tsx; then
    echo -e "${RED}Error: could not update the version in src/index.tsx${NC}"
    echo "The About modal's version string may have been renamed."
    exit 1
fi
echo -e "${GREEN}✓ About modal in src/index.tsx${NC}"

# dist/ is gitignored, so it MUST be built here or the zip ships with no frontend
echo ""
echo -e "${YELLOW}Building...${NC}"
$PKG_RUN build
if [ ! -f dist/index.js ]; then
    echo -e "${RED}Error: dist/index.js missing after build${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Build complete${NC}"

rm -f allycenter-v*.zip
ZIP_NAME="allycenter-v${VERSION}.zip"

echo ""
echo -e "${YELLOW}Creating ${ZIP_NAME}${NC}"
# Files sit at the zip root because install.sh extracts straight into
# ~/homebrew/plugins/Ally Center/
zip -rq "$ZIP_NAME" dist main.py plugin.json package.json LICENSE README.md defaults icons \
    -x "*.DS_Store"
echo -e "${GREEN}✓ ${ZIP_NAME}$(du -h "$ZIP_NAME" | cut -f1 | sed 's/^/ (/;s/$/)/')${NC}"

unzip -l "$ZIP_NAME" | tail -n +2 | head -20

echo ""
echo -e "${GREEN}=== Release v${VERSION} packaged ===${NC}"
echo ""
echo "Next: commit the version bump, then publish so install.sh can find it."
echo "install.sh downloads the latest GitHub release, so an unpublished zip"
echo "means 'curl | sh' installs nothing."
echo ""

if command -v gh >/dev/null 2>&1; then
    read -r -p "Create GitHub release v${VERSION} now? (y/N): " -n 1 REPLY; echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        gh release create "v${VERSION}" "$ZIP_NAME" \
            --title "v${VERSION}" \
            --notes "See CHANGELOG.md for details."
        echo -e "${GREEN}✓ Published${NC}"
    fi
else
    echo "gh CLI not found - upload ${ZIP_NAME} to a GitHub release manually."
fi
