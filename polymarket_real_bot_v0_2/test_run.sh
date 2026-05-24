#!/bin/bash
# Chain Gambler v0.3 — Build & Test Runner
# Run this from your terminal (needs Full Disk Access for rustup)

set -e

echo "╔════════════════════════════════════════╗"
echo "║  CHAIN GAMBLER v0.3 — BUILD & TEST    ║"
echo "╚════════════════════════════════════════╝"
echo ""

cd /Volumes/AI_DRIVE/polymarket/polymarket_real_bot_v0_1

echo "Step 1: Building release binary..."
cargo build --release 2>&1
echo "✅ Build complete"
echo ""

echo "Step 2: Testing market discovery..."
cargo run --release -- markets --config config.toml 2>&1
echo ""
echo "✅ Market discovery test complete"
echo ""

echo "Step 3: Testing order book depth..."
cargo run --release -- depth --config config.toml 2>&1
echo ""
echo "✅ Depth test complete"
echo ""

echo "════════════════════════════════════════"
echo "Ready to go live? Run:"
echo "  cargo run --release -- run --live --config config.toml"
echo "════════════════════════════════════════"
