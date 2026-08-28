from __future__ import annotations

from .types import Cost, Model, Usage

_PER_MILLION = 1_000_000


def calculate_cost(model: Model, usage: Usage) -> Cost:
    c = model.cost
    inp = usage.input * c.input / _PER_MILLION
    out = usage.output * c.output / _PER_MILLION
    cr = usage.cache_read * c.cache_read / _PER_MILLION
    cw = usage.cache_write * c.cache_write / _PER_MILLION

    return Cost(input=inp, output=out, cache_read=cr, cache_write=cw, total=inp + out + cr + cw)


def apply_cost(model: Model, usage: Usage) -> Usage:
    usage.total_tokens = usage.input + usage.output + usage.cache_read + usage.cache_write
    usage.cost = calculate_cost(model, usage)
    return usage
