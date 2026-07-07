# RG Billing

> **Part of the [ResonantGenesis](https://resonant.dev-swat.com) platform** — Credits, payments, and usage tracking.

[![Status: Production](https://img.shields.io/badge/Status-Production-brightgreen.svg)]()
[![License: RG Source Available](https://img.shields.io/badge/License-RG%20Source%20Available-blue.svg)](LICENSE.txt)

## Features
- Credit balance management
- Stripe integration for payments
- Plan management (free/pro/enterprise)
- Per-action usage tracking and deduction

## Quick Start
```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Deployment
- **Container**: `billing_service` | **Port**: 8000
- **Server path**: `/home/deploy/RG_Billing`

---
**Organization**: [DevSwat-ResonantGenesis](https://github.com/DevSwat-ResonantGenesis) | **Platform**: [resonant.dev-swat.com](https://resonant.dev-swat.com)
