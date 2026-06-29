# This file makes 'app' a proper Python package
import os
import sys

package_dir = os.path.dirname(os.path.abspath(__file__))
if package_dir not in sys.path:
    sys.path.insert(0, package_dir)

try:
    from .app import app, create_app
    from .config import Config
    __all__ = ['app', 'create_app', 'Config']
except ImportError:
    # Allow direct script execution
    pass
