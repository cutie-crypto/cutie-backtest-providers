"""Cutie Backtest Provider Validator (IMPL W3.9 §8).

CLI that checks a Cutie backtest provider's HTTP response layer against the
W3.9 v1 contract. Exposed as the ``cutie-backtest-provider-validator`` command.
"""

from .validator import ProviderValidator, SmokeParams, ValidationReport

__all__ = ["ProviderValidator", "SmokeParams", "ValidationReport"]
__version__ = "1.0.0"
