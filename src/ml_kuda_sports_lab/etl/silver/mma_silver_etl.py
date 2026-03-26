#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Silver Layer ETL runner.

The Silver ETL implementation currently lives in:
  ml_kuda_sports_lab.etl.silver.mma_silver_schema

This module exists so Docker can run:
  python -m ml_kuda_sports_lab.etl.silver.mma_silver_etl
"""

from __future__ import annotations

from ml_kuda_sports_lab.etl.silver.mma_silver_schema import SilverLayerETL, main

__all__ = ["SilverLayerETL", "main"]


if __name__ == "__main__":
    main()
