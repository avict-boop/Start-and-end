"""Анализатор соответствия состава БАД требованиям законодательства РФ/ЕАЭС."""
from .analyzer import analyze, Analyzer
from .parser import load_product, parse_composition_text, product_from_dict

__all__ = ["analyze", "Analyzer", "load_product", "parse_composition_text", "product_from_dict"]
__version__ = "0.1.0"
