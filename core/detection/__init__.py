"""Layer 3 false-data-injection detector (TSYP14, Track 2).

Modules, in pipeline order: ``data`` (cache, partitions, episodes), ``attacks`` (synthetic training
attacks), ``features`` (causal feature extractor), ``model`` (Extra Trees, calibration, alert rule,
``Detector``), ``metrics`` and ``pipeline`` (``python -m core.detection.pipeline``).
"""
