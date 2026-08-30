# Universal Bounty Engine V2

Monolithic Orchestrator & Ephemeral Execution Fleet.

Consolidates legacy PM2 background microservices into a unified, scheduled Hourly Sweeper pipeline.

## Features
- **Hourly Sweeper**: 6-phase pipeline executing Inbox -> Escort -> Sync -> Intake -> Execution -> Teardown.
- **Core Security & PathGuard**: Absolute non-bypassable filesystem containment protecting designated ignore list against symlink, firmlink, and traversal attacks.
- **Safe I/O**: Atomic file operations and streaming JSONL processing with rigorous PathGuard checks.
- **Firestore ACID Client**: Full transaction and document lifecycle management with automatic offline JSONL fallback.
- **State Migration Utility**: `migrate_queues` CLI & API for lossless migration of legacy queues and Firestore records.
- **Dual-Chain Payout Routing**: Hardcoded EVM and Stellar payout addresses enforced across all PRs.
