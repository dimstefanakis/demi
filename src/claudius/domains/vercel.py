from __future__ import annotations

from dataclasses import dataclass
import re
import subprocess

from claudius.config import Settings


@dataclass(frozen=True)
class DomainQuote:
    domain: str
    available: bool
    price_usd: float | None
    currency: str | None
    raw_output: str
    error: str | None = None


@dataclass(frozen=True)
class DomainPurchaseResult:
    domain: str
    success: bool
    raw_output: str
    error: str | None = None


def quote_domain(domain: str, settings: Settings) -> DomainQuote:
    output = _run_vercel_domains_buy(domain, settings, confirm=False)
    price = _parse_price(output)
    if "Invalid domain name" in output:
        return DomainQuote(domain, False, None, None, output, error="invalid_domain")
    if "Domain price not found" in output:
        return DomainQuote(domain, False, None, None, output, error="price_not_found")
    if "not available" in output.lower() or "unavailable" in output.lower():
        return DomainQuote(domain, False, None, None, output, error="unavailable")
    if price is not None and "available" in output.lower():
        return DomainQuote(domain, True, price, "USD", output)
    if price is not None:
        return DomainQuote(domain, True, price, "USD", output)
    return DomainQuote(domain, False, None, None, output, error="unknown")


def buy_domain(domain: str, settings: Settings) -> DomainPurchaseResult:
    output = _run_vercel_domains_buy(domain, settings, confirm=True)
    if "Error:" in output or "error:" in output:
        return DomainPurchaseResult(domain, False, output, error="vercel_error")
    return DomainPurchaseResult(domain, True, output)


def _run_vercel_domains_buy(domain: str, settings: Settings, confirm: bool) -> str:
    cmd = [settings.resolved_vercel_cmd(), "domains", "buy", domain]
    if settings.vercel_token:
        cmd.extend(["--token", settings.vercel_token])
    if settings.vercel_scope:
        cmd.extend(["--scope", settings.vercel_scope])
    response = "y\n" if confirm else "n\n"
    completed = subprocess.run(
        cmd,
        input=response,
        text=True,
        capture_output=True,
        check=False,
    )
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    return "\n".join([stdout, stderr]).strip()


def _parse_price(output: str) -> float | None:
    match = re.search(r"Buy now for \$([0-9]+(?:\.[0-9]+)?)", output)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None
