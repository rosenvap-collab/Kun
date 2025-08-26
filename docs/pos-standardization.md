# POS Menu Standardization

This document describes how menu items from different POS vendors are mapped to a single canonical schema.

## Setup

1. Install dependencies with `npm install`.
2. Run the TypeScript compiler via `npm test` to ensure type safety.

## Canonical Schema

The canonical menu item lives in [`models/PosMenuItem.ts`](../models/PosMenuItem.ts) and defines:

- `canonicalName` – standardized name for the item.
- `aliases` – list of alternative names.
- `category` – high level grouping such as "drink" or "entree".
- `attributes` – free-form key/value pairs like modifiers or tags.

## Vendor Adapters

Each integration under `integrations/pos` maps vendor data into the canonical schema. For example:

- [`integrations/pos/toast.ts`](../integrations/pos/toast.ts)
- [`integrations/pos/square.ts`](../integrations/pos/square.ts)

Adapters should return arrays of `PosMenuItem` objects.

## Standardization Service

The service [`services/posMenuStandardizer.ts`](../services/posMenuStandardizer.ts) tokenizes item names and uses Jaccard similarity scoring to match vendor items to canonical entries.

### Mapping Rules

- Tokenization is case-insensitive and splits on non-alphanumeric characters.
- Items are considered a match when their similarity score exceeds the threshold (default 0.5).
- Vendor-specific details that do not affect matching go into the `attributes` field.
