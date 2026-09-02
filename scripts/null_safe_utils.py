#!/usr/bin/env python3
"""
Null-safe utility functions for defensive data access in visualization scripts.

Provides graceful fallbacks for missing/None values without hiding real data gaps.
All functions print "N/A" or return safe defaults when data is absent.

Usage:
    from null_safe_utils import safe_get, safe_get_dict, safe_load_json

    # Safe dict access
    best = safe_get_dict(ilp, 'best', None)
    certified = safe_get(best, 'certified_rows_sample', 0) if best else 0

    # Safe file loading
    map_metrics = safe_load_json('out/doc.extraction.map_metrics.v1.json', {})
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, TypeVar, Union

T = TypeVar('T')


def safe_get(d: Optional[Dict[str, Any]], key: str, default: T = None) -> T:
    """
    Safely get a value from a dict, handling None dicts and missing keys.

    Args:
        d: Dictionary to access (may be None)
        key: Key to retrieve
        default: Default value if dict is None or key missing

    Returns:
        Value from dict[key] or default

    Examples:
        >>> safe_get({'a': 1}, 'a', 0)
        1
        >>> safe_get(None, 'a', 0)
        0
        >>> safe_get({'a': 1}, 'b', 'N/A')
        'N/A'
    """
    if d is None:
        return default
    return d.get(key, default)


def safe_get_dict(d: Optional[Dict[str, Any]], key: str, default: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Safely get a dict value from a dict, handling None dicts and missing keys.

    Args:
        d: Dictionary to access (may be None)
        key: Key to retrieve
        default: Default dict if key missing or value is None

    Returns:
        Dict from d[key] or default

    Examples:
        >>> safe_get_dict({'ilp': {'best': {}}}, 'ilp', {})
        {'best': {}}
        >>> safe_get_dict(None, 'ilp', {})
        {}
        >>> safe_get_dict({'ilp': None}, 'ilp', {})
        {}
    """
    if d is None:
        return default
    val = d.get(key, default)
    return val if val is not None else default


def safe_get_list(d: Optional[Dict[str, Any]], key: str, default: Optional[List[Any]] = None) -> List[Any]:
    """
    Safely get a list value from a dict, handling None dicts and missing keys.

    Args:
        d: Dictionary to access (may be None)
        key: Key to retrieve
        default: Default list if key missing or value is None

    Returns:
        List from d[key] or default

    Examples:
        >>> safe_get_list({'services': [1, 2]}, 'services', [])
        [1, 2]
        >>> safe_get_list(None, 'services', [])
        []
        >>> safe_get_list({'services': None}, 'services', [])
        []
    """
    if default is None:
        default = []
    if d is None:
        return default
    val = d.get(key, default)
    return val if val is not None else default


def safe_get_int(d: Optional[Dict[str, Any]], key: str, default: int = 0) -> int:
    """
    Safely get an int value from a dict, with type coercion.

    Args:
        d: Dictionary to access (may be None)
        key: Key to retrieve
        default: Default int if key missing or value not coercible

    Returns:
        Int from d[key] or default

    Examples:
        >>> safe_get_int({'count': 5}, 'count', 0)
        5
        >>> safe_get_int({'count': '5'}, 'count', 0)
        5
        >>> safe_get_int(None, 'count', 0)
        0
        >>> safe_get_int({'count': 'N/A'}, 'count', 0)
        0
    """
    if d is None:
        return default
    val = d.get(key, default)
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def safe_get_float(d: Optional[Dict[str, Any]], key: str, default: float = 0.0) -> float:
    """
    Safely get a float value from a dict, with type coercion.

    Args:
        d: Dictionary to access (may be None)
        key: Key to retrieve
        default: Default float if key missing or value not coercible

    Returns:
        Float from d[key] or default

    Examples:
        >>> safe_get_float({'rate': 0.5}, 'rate', 0.0)
        0.5
        >>> safe_get_float({'rate': '0.5'}, 'rate', 0.0)
        0.5
        >>> safe_get_float(None, 'rate', 0.0)
        0.0
    """
    if d is None:
        return default
    val = d.get(key, default)
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_max(collection: Optional[Union[List[Any], Any]], default: Any = 0) -> Any:
    """
    Safely get max value from a collection, handling None/empty collections.

    Args:
        collection: Iterable to get max from (may be None or empty)
        default: Default value if collection is None/empty

    Returns:
        Max value from collection or default

    Examples:
        >>> safe_max([1, 2, 3], 0)
        3
        >>> safe_max([], 0)
        0
        >>> safe_max(None, 0)
        0
        >>> safe_max({1: 'a', 2: 'b'}.values(), 'N/A')
        'b'
    """
    if collection is None:
        return default
    try:
        # Handle dict_values and other iterables
        col_list = list(collection) if not isinstance(collection, list) else collection
        if not col_list:
            return default
        return max(col_list)
    except (ValueError, TypeError):
        return default


def safe_invert_dict(d: Optional[Dict[Any, Any]], default: Optional[Dict[Any, Any]] = None) -> Dict[Any, Any]:
    """
    Safely invert a dict (swap keys/values), handling None/empty dicts.

    Args:
        d: Dictionary to invert (may be None)
        default: Default dict if inversion fails

    Returns:
        Inverted dict {v: k for k, v in d.items()} or default

    Examples:
        >>> safe_invert_dict({0: 'a', 1: 'b'}, {})
        {'a': 0, 'b': 1}
        >>> safe_invert_dict(None, {})
        {}
        >>> safe_invert_dict({}, {})
        {}
    """
    if default is None:
        default = {}
    if d is None or not d:
        return default
    try:
        return {v: k for k, v in d.items()}
    except (AttributeError, TypeError):
        return default


def safe_load_json(path: Union[str, Path], default: T = None) -> T:
    """
    Safely load a JSON file, handling missing files and malformed JSON.

    Args:
        path: Path to JSON file
        default: Default value if file missing or JSON invalid

    Returns:
        Parsed JSON object or default

    Examples:
        >>> # With existing file
        >>> safe_load_json('existing.json', {})  # doctest: +SKIP
        {'key': 'value'}
        >>> # With missing file
        >>> safe_load_json('missing.json', {})
        {}
    """
    if path is None:
        return default
    try:
        p = Path(path)
        if not p.exists():
            return default
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, IOError):
        return default


if __name__ == "__main__":
    import doctest
    print("Running null_safe_utils.py doctests...")
    result = doctest.testmod()
    if result.failed == 0:
        print(f"✓ All {result.attempted} doctests passed")
    else:
        print(f"✗ {result.failed}/{result.attempted} doctests failed")
