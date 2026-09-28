"""nano-pay: self-custodied Nano (XNO) wallet + x402 payment client for AI agents."""

__version__ = "0.2.10"

RAW_PER_XNO = 10**30
_DECIMALS = 30  # 1 raw = 10**-30 XNO


class AmountError(ValueError):
    """An XNO amount that cannot be converted without losing value."""


def xno_to_raw(xno) -> int:
    """Convert an XNO amount to raw, exactly, or refuse.

    Nano's base unit is 1 raw = 10**-30 XNO. Two silent losses lived here:

    * `int(Decimal(str(xno)) * 10**30)` truncates anything finer than a raw —
      `xno_to_raw("0.0000000000000000000000000000005")` returned 0, so a
      deliberate half-raw payment became a payment of nothing.
    * the multiplication runs in the ambient `decimal` context, whose default
      precision is 28 significant digits while raw spans 31. A balance of
      `0.300000000000000000000000000007` XNO converted to
      `300000000000000000000000000000` raw, dropping the last digits.

    Both are avoided by reading the coefficient straight out of the decimal
    tuple — an exponent shift, not an arithmetic op — and by requiring the
    result to be integral. Non-finite, negative and unparseable input raise
    `AmountError` instead of surfacing an unrelated `decimal` exception.
    """
    from decimal import Decimal, InvalidOperation

    try:
        value = Decimal(str(xno))
    except InvalidOperation:
        raise AmountError(f"not a number: {xno!r}") from None
    if not value.is_finite():
        raise AmountError(f"not a finite amount: {xno!r}")
    if value < 0:
        raise AmountError(f"amount must not be negative: {xno!r}")

    sign, digits, exponent = value.as_tuple()
    coefficient = int("".join(map(str, digits)) or "0")
    raw = coefficient * 10 ** (exponent + _DECIMALS) if exponent + _DECIMALS >= 0 else None
    if raw is None:
        # More than 30 decimal places: not representable as whole raw.
        raise AmountError(
            f"{xno!r} XNO is smaller than one raw (10**-{_DECIMALS} XNO) and "
            "cannot be represented exactly"
        )
    return raw


def raw_to_xno(raw) -> str:
    """Render raw as XNO text, exactly.

    Arithmetic here would go through the ambient `decimal` context (default
    precision 28), which is narrower than raw's 31 digits, so a balance of
    `300000000000000000000000000007` raw rendered as `"0.3"`. The digits are
    therefore placed by string surgery on the decimal tuple, never divided.

    Trailing zeros are dropped, so `0.05` stays `0.05` rather than
    `0.0500…`, and a whole number stays whole.
    """
    from decimal import Decimal, InvalidOperation

    if isinstance(raw, bool) or not isinstance(raw, int):
        try:
            raw = int(str(raw).strip())
        except (TypeError, ValueError, InvalidOperation):
            raise AmountError(f"raw must be an integer, got {raw!r}") from None

    sign, digits, exponent = Decimal(raw).as_tuple()
    text = "".join(map(str, digits))
    point = len(text) + (exponent - _DECIMALS)
    if point <= 0:
        rendered = "0." + "0" * (-point) + text
    elif point >= len(text):
        rendered = text + "0" * (point - len(text))
    else:
        rendered = text[:point] + "." + text[point:]
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    rendered = rendered or "0"
    return ("-" + rendered) if sign else rendered
