"""
Security wrapper for Python code execution in the RAG chat interface.
Enforces import whitelisting and pattern checking.
"""
import ast
import os
import re
import string


ALLOWED_IMPORTS = {
    "pandas", "numpy", "matplotlib", "matplotlib.pyplot", "matplotlib.figure",
    "scipy", "scipy.stats", "scipy.sparse", "seaborn",
    "json", "csv", "math", "statistics", "datetime",
    "collections", "typing", "itertools", "functools",
    "io", "re", "os", "warnings", "base64", "hashlib",
}

FORBIDDEN_PATTERNS = [
    "__import__", "exec(", "eval(", "compile(",
    "subprocess", "socket", "requests.", "urllib",
    "os.system", "os.popen", "os.remove", "os.unlink",
    "shutil", "ctypes", "mmap", "pickle", "_pickle",
    "marshal", "site", "sys.modules", "importlib",
]

DANGEROUS_CALL_NAMES = {
    "system", "popen", "remove", "unlink", "rmdir", "removedirs",
    "rename", "renames", "replace", "spawnl", "spawnle", "spawnlp",
    "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
}
ALLOWED_TOP_LEVEL_IMPORTS = {name.split(".")[0] for name in ALLOWED_IMPORTS}


def check_code_safety(code: str) -> list[str]:
    """Check code for forbidden patterns and unapproved imports."""
    issues = []
    for pattern in FORBIDDEN_PATTERNS:
        if pattern in code:
            issues.append(f"Pattern proibito: {pattern}")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"Codice Python non valido: {exc.msg}"]

    imported_dangerous_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_TOP_LEVEL_IMPORTS:
                    issues.append(f"import {alias.name} non consentito")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            if top not in ALLOWED_TOP_LEVEL_IMPORTS:
                issues.append(f"import da {module} non consentito")
            if top == "os":
                for alias in node.names:
                    if alias.name in DANGEROUS_CALL_NAMES:
                        imported_dangerous_names.add(alias.asname or alias.name)
                        issues.append(f"os.{alias.name} non consentito")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == "os" and func.attr in DANGEROUS_CALL_NAMES:
                    issues.append(f"os.{func.attr} non consentito")
            elif isinstance(func, ast.Name) and func.id in imported_dangerous_names:
                issues.append(f"{func.id} non consentito")
    return issues


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal."""
    return "".join(c for c in filename if c in string.ascii_letters + string.digits + "._-")
