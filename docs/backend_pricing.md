# Managed Backend Pricing

Use this document to select the smallest tier that satisfies a user request.
Only choose from the tiers listed here. Do not invent tiers or prices.

## Tiers

| Compute Size | Hourly Price (USD) | Monthly Price (USD) | CPU | Memory | Max DB Size (Recommended) |
| --- | --- | --- | --- | --- | --- |
| Nano | 0 | 0 | Shared | Up to 0.5 GB | 500 MB |
| Micro | 0.01344 | ~10 | 2-core ARM (shared) | 1 GB | 10 GB |
| Small | 0.0206 | ~15 | 2-core ARM (shared) | 2 GB | 50 GB |
| Medium | 0.0822 | ~60 | 2-core ARM (shared) | 4 GB | 100 GB |
| Large | 0.1517 | ~110 | 2-core ARM (dedicated) | 8 GB | 200 GB |
| XL | 0.2877 | ~210 | 4-core ARM (dedicated) | 16 GB | 500 GB |
| 2XL | 0.562 | ~410 | 8-core ARM (dedicated) | 32 GB | 1 TB |
| 4XL | 1.32 | ~960 | 16-core ARM (dedicated) | 64 GB | 2 TB |
| 8XL | 2.562 | ~1,870 | 32-core ARM (dedicated) | 128 GB | 4 TB |
| 12XL | 3.836 | ~2,800 | 48-core ARM (dedicated) | 192 GB | 6 TB |
| 16XL | 5.12 | ~3,730 | 64-core ARM (dedicated) | 256 GB | 10 TB |

## Instance Size Codes
Use these lowercase codes when calling `provision_managed_backend`:
- nano, micro, small, medium, large, xl, 2xl, 4xl, 8xl, 12xl, 16xl

## Guidance
- Paid plan constraint: new instances cannot be Nano. Treat Nano as Micro pricing on paid plans and upgrade existing Nano projects to Micro when possible.
- Prefer the smallest tier that supports the requested features and expected data volume.
- If unsure, pick the lowest tier and explain upgrades are possible later.
