"""nanoclone: a Git client written from scratch on the standard library.

Implements enough of Git to clone a repository over HTTPS: the pkt-line wire
framing, the Smart HTTP v2 transport, the packfile format with delta
resolution, and a working-tree checkout with a valid index.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
