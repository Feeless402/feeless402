"""nano-pay: self-custodied Nano (XNO) wallet + x402 payment client for AI agents."""

__version__ = "0.2.3"

RAW_PER_XNO = 10**30


def xno_to_raw(xno) -> int:
    from decimal import Decimal

    return int(Decimal(str(xno)) * RAW_PER_XNO)


def raw_to_xno(raw: int) -> str:
    from decimal import Decimal

    return str((Decimal(raw) / RAW_PER_XNO).normalize())
