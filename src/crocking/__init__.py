"""crocking — AI authorship detector for git repositories."""
from .core import CommitAnalyzer, ScanReport, AuthorProfile, Signal, Confidence, main, __version__

__all__ = ["CommitAnalyzer", "ScanReport", "AuthorProfile", "Signal", "Confidence", "main", "__version__"]
